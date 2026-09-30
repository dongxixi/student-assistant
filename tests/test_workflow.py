import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from playwright.async_api import async_playwright
from app.main import create_app
from app.browser import Collector
from app.store import Store, now
from app.models import MailConfig


class IdleCollector(Collector):
    def start(self, sid):
        raise ValueError('测试中禁用定时调度')


@pytest.fixture
def client(tmp_path):
    app = create_app(tmp_path, IdleCollector)
    with TestClient(app) as c:
        c.headers['Authorization'] = 'Bearer ' + (tmp_path / 'admin.token').read_text()
        yield c


def add(client):
    r = client.post('/api/students', json={'name':'测试学生','email':'student@example.edu','password':'secret-password'})
    assert r.status_code == 200
    return r.json()['id']


def test_auth_encryption_and_origin(client):
    assert client.get('/api/students', headers={'Authorization':''}).status_code == 401
    sid = add(client)
    students = client.get('/api/students').json()
    assert 'secret' not in students[0] and 'password' not in students[0]
    store = client.app.state.store
    assert b'secret-password' not in store.rows('SELECT secret FROM students')[0]['secret']
    assert store.student(sid)['password'] == 'secret-password'
    assert client.post('/api/students/'+sid+'/toggle', headers={'Origin':'https://evil.test'}).status_code == 403
    assert client.post('/api/students',json={'name':'bad','email':'b','password':'x','config':{'login_url':'http://evil.test'}}).status_code == 422


def test_review_state_machine(client):
    sid = add(client)
    store = client.app.state.store
    store.execute('INSERT INTO messages VALUES(?,?,?,?,?,1)', ('m',sid,'key',store.seal({'body':'source'}),now()))
    store.execute('INSERT INTO items VALUES(?,?,?,?,?,?,?,?,NULL)', ('i','m',sid,'{}','提醒','pending_review',now(),now()))
    assert client.post('/api/items/i/sent').status_code == 409
    assert client.post('/api/items/i/approve').status_code == 200
    assert client.put('/api/items/i/draft',json={'draft':'修改'}).status_code == 200
    assert client.post('/api/items/i/sent').status_code == 409
    assert client.post('/api/items/i/approve').status_code == 200
    assert client.post('/api/items/i/sent').status_code == 200
    assert client.post('/api/items/i/sent').status_code == 409
    assert client.put('/api/items/i/draft',json={'draft':'再次修改'}).status_code == 409


async def test_dedup_and_retry(tmp_path, monkeypatch):
    monkeypatch.delenv('LLM_API_KEY', raising=False)
    store = Store(tmp_path)
    c = Collector(store, headless=True)
    s = {'id':'s','name':'学生','config':{'timezone':'Europe/London'}}
    mail={'subject':'作业要求','body':'Submit assignment by October 12.','source_key':'k'}
    assert await c.process(s,[mail]) == 1
    assert await c.process(s,[mail]) == 0
    assert len(store.rows('SELECT * FROM items')) == 1
    async def fail(*args):
        raise RuntimeError('model unavailable')
    monkeypatch.setattr('app.browser.extract',fail)
    mail2={**mail,'source_key':'k2'}
    with pytest.raises(RuntimeError):
        await c.process(s,[mail2])
    assert store.rows("SELECT extracted FROM messages WHERE source_key='k2'")[0]['extracted'] == 0
    from app.extraction import extract
    monkeypatch.setattr('app.browser.extract',extract)
    assert await c.process(s,[mail2]) == 1
    store.db.close()


def test_restart_recovery(tmp_path):
    s=Store(tmp_path)
    s.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?)',('j','s','waiting_verification','wait',now(),now()))
    s.db.close()
    s=Store(tmp_path)
    assert s.rows('SELECT status FROM jobs')[0]['status']=='interrupted'
    s.db.close()


PAGE = '''<!doctype html><html><body><div id="login"><input type="email"><input type="password"><button id="submit">Sign in</button></div><div id="mfa" hidden>Approve sign-in: <b>42</b><button id="approve">Approve on phone (test)</button></div><div id="inbox" hidden><button class="mail" data-message-id="one">Assignment</button><button class="mail" data-message-id="two">Exam</button><h2 id="subject"></h2><article id="body"></article></div><script>
function inbox(){document.querySelector('#login').hidden=true;document.querySelector('#mfa').hidden=true;document.querySelector('#inbox').hidden=false;}
if(localStorage.getItem('auth'))inbox();
document.querySelector('#submit').onclick=()=>{document.querySelector('#login').hidden=true;document.querySelector('#mfa').hidden=false};
document.querySelector('#approve').onclick=()=>{localStorage.setItem('auth','1');inbox();};
document.querySelectorAll('.mail').forEach((e,i)=>e.onclick=()=>{document.querySelector('#subject').textContent=i?'Exam':'Assignment';document.querySelector('#body').textContent=i?'Exam on November 1':'Assignment due October 12';});
</script></body></html>'''


@pytest.fixture
def mailbox_url():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200);self.send_header('Content-Type','text/html');self.end_headers();self.wfile.write(PAGE.encode())
        def log_message(self,*args): pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    yield f'http://127.0.0.1:{server.server_port}/'
    server.shutdown();thread.join();server.server_close()


async def test_browser_mfa_resume_state_and_isolation(tmp_path, mailbox_url):
    store=Store(tmp_path); c=Collector(store,headless=True)
    store.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?)',('j','s','logging_in','',now(),now()))
    config=MailConfig().model_dump()
    config.update(login_url=mailbox_url,inbox_url=mailbox_url,allowed_hosts=['127.0.0.1'],submit_selector='#submit',authenticated_selector='#inbox',message_selector='.mail',body_selector='#body',subject_selector='#subject',mfa_selector='#mfa',error_selector='',verification_timeout=10)
    s={'id':'s','name':'test','email':'test@example.edu','password':'secret','config':config}
    async with async_playwright() as p:
        browser=await p.chromium.launch(headless=True,args=['--no-sandbox'])
        context=await browser.new_context();page=await context.new_page()
        task=asyncio.create_task(c.login(page,s,'j'))
        for _ in range(50):
            if store.rows('SELECT status FROM jobs')[0]['status']=='waiting_verification':break
            await asyncio.sleep(.1)
        assert store.rows('SELECT status FROM jobs')[0]['status']=='waiting_verification'
        assert not task.done()
        assert await page.locator('#mfa').is_visible()
        await page.click('#approve')
        await asyncio.wait_for(task,5)
        mails=await c.collect(page,s)
        assert [m['subject'] for m in mails]==['Assignment','Exam']
        state=await context.storage_state(indexed_db=True)
        restored=await browser.new_context(storage_state=state);restored_page=await restored.new_page()
        await restored_page.goto(mailbox_url)
        assert await c.authenticated(restored_page,config)
        isolated=await browser.new_context();other=await isolated.new_page();await other.goto(mailbox_url)
        assert not await c.authenticated(other,config)
        await browser.close()
    store.db.close()


async def test_verification_timeout(tmp_path, mailbox_url):
    store=Store(tmp_path);c=Collector(store,headless=True)
    store.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?)',('j','s','logging_in','',now(),now()))
    config=MailConfig().model_dump();config.update(login_url=mailbox_url,inbox_url=mailbox_url,allowed_hosts=['127.0.0.1'],submit_selector='#submit',authenticated_selector='#inbox',mfa_selector='#mfa',error_selector='',verification_timeout=2)
    s={'email':'x','password':'p','config':config}
    async with async_playwright() as p:
        browser=await p.chromium.launch(headless=True,args=['--no-sandbox']);context=await browser.new_context();page=await context.new_page()
        with pytest.raises(TimeoutError):await c.login(page,s,'j')
        assert not await c.authenticated(page,config)
        await browser.close()
    store.db.close()


def test_start_duplicate_cancel_and_failed_status(tmp_path, monkeypatch):
    async def slow(self, sid, jid):
        try:
            self.update(jid,'waiting_verification','test')
            await asyncio.sleep(100)
        except asyncio.CancelledError:
            self.update(jid,'cancelled','cancelled')
            raise
    monkeypatch.setattr(Collector,'run',slow)
    app=create_app(tmp_path)
    with TestClient(app) as c:
        c.headers['Authorization']='Bearer '+(tmp_path/'admin.token').read_text()
        sid=add(c)
        first=c.post('/api/students/'+sid+'/collect')
        assert first.status_code==200
        assert c.post('/api/students/'+sid+'/collect').status_code==409
        jid=first.json()['job_id']
        assert c.post('/api/jobs/'+jid+'/cancel').status_code==200
        import time
        for _ in range(20):
            if c.get('/api/jobs').json()[0]['status']=='cancelled':break
            time.sleep(.01)
        assert c.get('/api/jobs').json()[0]['status']=='cancelled'


async def test_extraction_evidence_and_timezone(monkeypatch):
    from app.extraction import extract
    monkeypatch.setenv('LLM_API_KEY','test')
    item={'title':'Essay','category':'assignment','deadline_local':'2026-10-12T17:00:00','timezone':'Europe/London','weight_percent':20,'required_action':'提交论文','evidence':'Essay due 12 October 2026 at 17:00, worth 20%.'}
    class Reply:
        def raise_for_status(self):pass
        def json(self):return {'choices':[{'message':{'content':json.dumps({'items':[item]})}}]}
    class FakeClient:
        def __init__(self,*a,**k):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*a):pass
        async def post(self,*a,**k):return Reply()
    monkeypatch.setattr('app.extraction.httpx.AsyncClient',FakeClient)
    mail={'subject':'Essay','body':item['evidence']}
    result=await extract(mail,'Europe/London')
    assert result[0]['deadline_beijing']=='2026-10-13T00:00:00+08:00'
    item['evidence']='not in original'
    with pytest.raises(ValueError):await extract(mail,'Europe/London')


async def test_dashboard_browser(tmp_path):
    import socket
    import uvicorn
    from playwright.async_api import expect
    app=create_app(tmp_path,IdleCollector)
    sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    server=uvicorn.Server(uvicorn.Config(app,log_level='error'))
    thread=threading.Thread(target=lambda:server.run(sockets=[sock]),daemon=True);thread.start()
    try:
        for _ in range(100):
            if server.started:break
            await asyncio.sleep(.05)
        assert server.started
        async with async_playwright() as p:
            browser=await p.chromium.launch(headless=True,args=['--no-sandbox'])
            page=await browser.new_page(viewport={'width':1440,'height':1000})
            errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
            await page.goto(f'http://127.0.0.1:{port}/')
            await page.fill('#token',(tmp_path/'admin.token').read_text())
            await page.click('#connect')
            await expect(page.locator('#n-students')).to_have_text('0')
            await page.click('#add')
            await page.fill('#name','测试学生')
            await page.fill('#email','student@example.edu')
            await page.fill('#password','test-password')
            await page.locator('#student-form button[type=submit]').click()
            await expect(page.locator('#n-students')).to_have_text('1')
            await expect(page.locator('#students')).to_contain_text('测试学生')
            await page.screenshot(path='/tmp/student-dashboard.png',full_page=True)
            assert not errors
            await browser.close()
    finally:
        server.should_exit=True
        await asyncio.to_thread(thread.join,5)
        sock.close()
