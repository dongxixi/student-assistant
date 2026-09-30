import asyncio
import hmac
import json
import os
import secrets
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from .browser import Collector, ACTIVE
from .models import StudentInput, DraftInput
from .store import Store, now


def create_app(root=None, collector_factory=Collector):
    store = Store(root or os.getenv('DATA_DIR', 'data'))
    token_file = store.root / 'admin.token'
    if not token_file.exists():
        fd = os.open(token_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'w') as f:
            f.write(secrets.token_urlsafe(32))
    token = token_file.read_text().strip()
    collector = collector_factory(store)

    async def schedule():
        while True:
            for s in store.rows('SELECT id,config FROM students WHERE enabled=1'):
                config = json.loads(s['config'])
                latest = store.rows('SELECT updated FROM jobs WHERE student_id=? ORDER BY created DESC LIMIT 1', (s['id'],))
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(latest[0]['updated'])).total_seconds() if latest else float('inf')
                if age >= config['interval_minutes'] * 60:
                    try:
                        collector.start(s['id'])
                    except ValueError:
                        pass
            await asyncio.sleep(15)

    @asynccontextmanager
    async def lifespan(app):
        scheduler = asyncio.create_task(schedule())
        yield
        scheduler.cancel()
        await asyncio.gather(scheduler, return_exceptions=True)
        await collector.stop()
        store.db.close()

    app = FastAPI(title='学管助手第一期', lifespan=lifespan)
    app.state.store, app.state.collector = store, collector

    @app.middleware('http')
    async def protect(request: Request, call_next):
        if request.url.path.startswith('/api/'):
            supplied = request.headers.get('Authorization', '').removeprefix('Bearer ')
            if not hmac.compare_digest(supplied, token):
                return JSONResponse({'detail': '请填写管理员令牌'}, status_code=401)
            if request.method not in ('GET', 'HEAD'):
                origin = request.headers.get('origin')
                if origin and origin != str(request.base_url).rstrip('/'):
                    return JSONResponse({'detail': '拒绝跨站请求'}, status_code=403)
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'"
        return response

    @app.get('/')
    def index():
        return FileResponse(Path(__file__).parent / 'static/index.html')

    @app.get('/static/{name}')
    def static(name: str):
        if name not in ('app.js', 'style.css'):
            raise HTTPException(404)
        return FileResponse(Path(__file__).parent / 'static' / name)

    @app.get('/api/students')
    def students():
        rows = store.rows('SELECT id,name,email,config,enabled FROM students')
        for row in rows:
            row['config'] = json.loads(row['config'])
        return rows

    @app.post('/api/students')
    def add_student(body: StudentInput):
        from urllib.parse import urlparse
        config = body.config.model_dump()
        if any(urlparse(config[k]).hostname not in config['allowed_hosts'] for k in ('login_url', 'inbox_url')):
            raise HTTPException(422, '登录与邮箱域名必须在 allowed_hosts 中')
        sid = uuid.uuid4().hex
        try:
            store.execute('INSERT INTO students(id,name,email,secret,config) VALUES(?,?,?,?,?)', (sid, body.name, body.email, store.seal(body.password), json.dumps(config)))
        except Exception:
            raise HTTPException(409, '邮箱已存在或保存失败')
        store.audit('student_created', sid)
        return {'id': sid}

    @app.put('/api/students/{sid}')
    def edit_student(sid: str, body: StudentInput):
        from urllib.parse import urlparse
        require_student(sid)
        if sid in collector.tasks and not collector.tasks[sid].done():
            raise HTTPException(409, '请先取消正在执行的任务')
        c = body.config.model_dump()
        if any(urlparse(c[k]).hostname not in c['allowed_hosts'] for k in ('login_url','inbox_url')):
            raise HTTPException(422, '域名未列入 allowed_hosts')
        try:
            store.execute('UPDATE students SET name=?,email=?,secret=?,config=? WHERE id=?', (body.name, body.email, store.seal(body.password), json.dumps(c), sid))
        except Exception:
            raise HTTPException(409, '邮箱已存在或保存失败')
        (store.root / (sid + '.state')).unlink(missing_ok=True)
        store.audit('student_updated', sid)
        return {'ok': True}

    def require_student(sid):
        try:
            return store.student(sid)
        except KeyError:
            raise HTTPException(404, '学生不存在')

    @app.post('/api/students/{sid}/collect')
    async def collect(sid: str):
        require_student(sid)
        try:
            return {'job_id': collector.start(sid)}
        except ValueError as exc:
            raise HTTPException(409, str(exc))

    @app.post('/api/students/{sid}/toggle')
    def toggle(sid: str):
        require_student(sid)
        store.execute('UPDATE students SET enabled=1-enabled WHERE id=?', (sid,))
        return {'ok': True}

    @app.get('/api/jobs')
    def jobs():
        return store.rows('SELECT j.*,s.name,s.email FROM jobs j JOIN students s ON j.student_id=s.id ORDER BY created DESC LIMIT 100')

    @app.get('/api/jobs/{jid}/screenshot')
    def screenshot(jid: str):
        view = collector.views.get(jid)
        if not view:
            raise HTTPException(404, '当前任务没有验证截图')
        return Response(view['image'], media_type='image/png')

    @app.post('/api/jobs/{jid}/cancel')
    async def cancel(jid: str):
        jobs = store.rows('SELECT * FROM jobs WHERE id=?', (jid,))
        if not jobs:
            raise HTTPException(404)
        task = collector.tasks.get(jobs[0]['student_id'])
        latest = store.rows('SELECT id FROM jobs WHERE student_id=? ORDER BY created DESC LIMIT 1', (jobs[0]['student_id'],))
        if jobs[0]['status'] not in ACTIVE or not latest or latest[0]['id'] != jid:
            raise HTTPException(409, '任务已经结束')
        if task:
            task.cancel()
        return {'ok': True}

    @app.get('/api/items')
    def items():
        rows = store.rows('SELECT i.*,s.name FROM items i JOIN students s ON i.student_id=s.id ORDER BY created DESC LIMIT 300')
        for row in rows:
            row['payload'] = json.loads(row['payload'])
        return rows

    @app.get('/api/messages/{mid}')
    def message(mid: str):
        rows = store.rows('SELECT payload FROM messages WHERE id=?', (mid,))
        if not rows:
            raise HTTPException(404)
        return store.unseal(rows[0]['payload'])

    @app.put('/api/items/{iid}/draft')
    def edit(iid: str, body: DraftInput):
        changed = store.execute("UPDATE items SET draft=?,status='pending_review',updated=? WHERE id=? AND status!='sent'", (body.draft, now(), iid))
        if not changed:
            raise HTTPException(409, '事项不存在或已发送')
        store.audit('draft_edited', iid)
        return {'ok': True}

    @app.post('/api/items/{iid}/{action}')
    def action(iid: str, action: str):
        transitions = {'approve': ('pending_review', 'approved'), 'sent': ('approved', 'sent'), 'dismiss': ('pending_review', 'dismissed')}
        if action not in transitions:
            raise HTTPException(404)
        old, new = transitions[action]
        changed = store.execute('UPDATE items SET status=?,updated=?,sent_at=? WHERE id=? AND status=?', (new, now(), now() if new == 'sent' else None, iid, old))
        if not changed:
            raise HTTPException(409, '事项状态不允许该操作')
        store.audit(action, iid)
        return {'ok': True}

    return app
