import asyncio
import hashlib
import json
import os
import re
import time
import uuid
from contextlib import suppress
from urllib.parse import urlparse
from playwright.async_api import async_playwright, Error as BrowserError, TimeoutError as BrowserTimeout
from .extraction import extract, draft_for
from .store import now

ACTIVE = ('queued', 'logging_in', 'waiting_verification', 'collecting', 'diagnosing')


class Collector:
    def __init__(self, store, headless=None):
        self.store = store
        self.headless = (os.getenv('HEADLESS', 'false').lower() == 'true') if headless is None else headless
        self.tasks = {}
        self.views = {}
        self.blocked = {}
        self.limit = asyncio.Semaphore(int(os.getenv('MAX_BROWSERS', '3')))

    def start(self, sid):
        self.store.student(sid)
        if sid in self.tasks and not self.tasks[sid].done():
            raise ValueError('该学生已有采集任务')
        jid = uuid.uuid4().hex
        self.store.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?)', (jid, sid, 'queued', '等待浏览器资源', now(), now()))
        self.tasks[sid] = asyncio.create_task(self.run(sid, jid))
        return jid

    def update(self, jid, status, detail):
        self.store.execute('UPDATE jobs SET status=?,detail=?,updated=? WHERE id=?', (status, detail, now(), jid))

    async def visible(self, page, selector):
        if not selector:
            return False
        locator = page.locator(selector)
        for index in range(min(await locator.count(), 20)):
            if await locator.nth(index).is_visible():
                return True
        return False

    async def first_visible(self, page, selector):
        locator = page.locator(selector)
        for index in range(min(await locator.count(), 20)):
            if await locator.nth(index).is_visible():
                return locator.nth(index)
        return None

    async def authenticated(self, page, config):
        hosts = {urlparse(config['inbox_url']).hostname}
        if config.get('provider') == 'outlook':
            hosts.update({'outlook.office.com', 'outlook.office365.com'})
        return (urlparse(page.url).hostname in hosts
                and await self.visible(page, config['authenticated_selector']))

    async def snapshot(self, page, jid):
        # Redirects/font loading can temporarily prevent screenshots. Preview failure
        # must never terminate a login attempt.
        try:
            self.views[jid] = {'image': await page.screenshot(type='png', timeout=3000), 'url': page.url}
        except BrowserError:
            pass

    def failure_detail(self, exc, jid):
        blocked = self.blocked.get(jid)
        if blocked:
            return f'登录跳转被拦截：{blocked}。确认是学校 SSO 后，将该域名加入 allowed_hosts。'
        network = re.search(r'net::ERR_[A-Z_]+', str(exc))
        if network:
            return '浏览器网络错误：' + network.group() + '。请检查网络、代理或证书。'
        if isinstance(exc, BrowserTimeout):
            return '页面加载或元素等待超时，请检查网络或页面选择器。'
        if isinstance(exc, (RuntimeError, TimeoutError, ValueError)) and not isinstance(exc, BrowserError):
            return str(exc)[:250]
        return '浏览器或模型调用失败（' + type(exc).__name__ + '），请检查配置。'

    async def diagnose(self, page, student, jid, exc):
        seconds = student['config'].get('failure_keep_seconds', 120)
        detail = self.failure_detail(exc, jid)
        self.update(jid, 'diagnosing', detail + f' 浏览器保留 {seconds} 秒供排查，可取消任务关闭。')
        end = time.monotonic() + seconds
        while not page.is_closed() and time.monotonic() < end:
            await self.snapshot(page, jid)
            await asyncio.sleep(1)

    async def login(self, page, student, jid):
        c = student['config']
        # Commit only waits for navigation to start; authentication is checked below.
        # DOMContentLoaded may be delayed by SSO resources and does not prove readiness.
        try:
            await page.goto(c['login_url'], wait_until='commit', timeout=60000)
        except BrowserError:
            if page.is_closed() or page.url == 'about:blank' or self.blocked.get(jid):
                raise
            # A redirected document may already be usable even if goto was interrupted.
            self.update(jid, 'logging_in', '页面仍在跳转或加载，继续等待登录界面')
        end = time.monotonic() + c['verification_timeout']
        submitted = set()
        waiting = False
        while time.monotonic() < end:
            if page.is_closed():
                raise RuntimeError('浏览器已关闭')
            if page.url == 'about:blank':
                await asyncio.sleep(1)
                continue
            if urlparse(page.url).hostname not in c['allowed_hosts']:
                raise RuntimeError('登录跳转到未授权域名，请补充受信任的学校SSO域名')
            try:
                if await self.authenticated(page, c):
                    return
                error = await self.first_visible(page, c['error_selector']) if c['error_selector'] else None
                if error and (await error.inner_text()).strip():
                    raise RuntimeError('页面提示登录错误，请检查账号或密码')
                mfa = await self.visible(page, c['mfa_selector'])
                if mfa:
                    waiting = True
                    self.update(jid, 'waiting_verification', '请在手机批准登录；数字匹配或验证码请查看并操作原浏览器窗口')
                if not mfa and not waiting:
                    field = await self.first_visible(page, c['username_selector'])
                    password = await self.first_visible(page, c['password_selector'])
                    kind = 'password' if password else 'username'
                    field = password or field
                    if field and kind not in submitted:
                        if password:
                            username = await self.first_visible(page, c['username_selector'])
                            if username:
                                await username.fill(student['email'])
                                submitted.add('username')
                        await field.fill(student['password'] if password else student['email'])
                        submit = await self.first_visible(page, c['submit_selector'])
                        if submit:
                            await submit.click()
                        else:
                            await field.press('Enter')
                        submitted.add(kind)
                    elif not field:
                        # SSO account selection, CAPTCHA and unfamiliar challenges are handled in the same window.
                        self.update(jid, 'waiting_verification', '登录尚未完成，请查看原浏览器窗口；系统确认进入邮箱后会自动继续')
            except BrowserError as exc:
                if page.is_closed():
                    raise RuntimeError('浏览器已关闭') from exc
                if 'selector' in str(exc).lower() and ('invalid' in str(exc).lower() or 'unexpected token' in str(exc).lower()):
                    raise RuntimeError('页面选择器格式错误，请检查邮箱配置') from exc
                self.update(jid, 'logging_in', '页面正在加载或跳转，继续等待；可在原浏览器操作')
            await self.snapshot(page, jid)
            await asyncio.sleep(1)
        raise TimeoutError('登录或验证超时，请重新采集')

    async def collect(self, page, student):
        c = student['config']
        await page.goto(c['inbox_url'], wait_until='domcontentloaded')
        await page.locator(c['authenticated_selector']).first.wait_for(state='visible', timeout=30000)
        # A list absent due to selector drift must never be reported as an empty mailbox.
        end = time.monotonic() + 30
        while time.monotonic() < end:
            if await page.locator(c['message_selector']).count():
                break
            if c['empty_selector'] and await self.visible(page, c['empty_selector']):
                return []
            await asyncio.sleep(.5)
        rows = page.locator(c['message_selector'])
        count = min(await rows.count(), c['max_messages'])
        if not count:
            raise RuntimeError('未找到邮件列表，需检查邮箱适配器选择器，不能认定为空邮箱')
        messages = []
        previous_body = None
        for i in range(count):
            row = rows.nth(i)
            identity = await row.get_attribute('data-convid') or await row.get_attribute('data-message-id')
            summary = await row.inner_text()
            await row.click()
            body = page.locator(c['body_selector']).first
            await body.wait_for(state='visible', timeout=15000)
            # Fail closed if the reading pane never changes after selecting another row.
            if previous_body is not None:
                await page.wait_for_function(
                    '(arg) => {const el=document.querySelector(arg.selector); return el && el.innerText.trim() !== arg.previous.trim();}',
                    arg={'selector': c['body_selector'], 'previous': previous_body}, timeout=15000)
            await asyncio.sleep(.5)
            text = await body.inner_text()
            previous_body = text
            if not text.strip():
                raise RuntimeError('邮件正文为空，需人工检查')
            subjects = page.locator(c['subject_selector'])
            subject = (await subjects.last.inner_text()) if await subjects.count() else summary.split('\n')[0]
            sender = ''
            if c['sender_selector'] and await page.locator(c['sender_selector']).count():
                sender = await page.locator(c['sender_selector']).first.inner_text()
            payload = {'subject': subject, 'sender': sender, 'body': text, 'source_url': page.url,
                       'list_summary': summary, 'captured_at': now()}
            # Include content hash so edits/new messages inside a conversation become a new version.
            key = hashlib.sha256((str(identity or '') + subject + text).encode()).hexdigest()
            payload['source_key'] = key
            messages.append(payload)
        return messages

    async def process(self, student, messages):
        created = 0
        for message in messages:
            mid = uuid.uuid4().hex
            added = self.store.execute('INSERT OR IGNORE INTO messages VALUES(?,?,?,?,?,0)',
                                      (mid, student['id'], message['source_key'], self.store.seal(message), now()))
            if not added:
                old = self.store.rows('SELECT id,extracted FROM messages WHERE student_id=? AND source_key=?', (student['id'], message['source_key']))[0]
                if old['extracted']:
                    continue
                mid = old['id']
            items = await extract(message, student['config']['timezone'])
            # Transaction: all items or none; retries must not create duplicates.
            with self.store.lock:
                try:
                    for item in items:
                        self.store.db.execute('INSERT INTO items VALUES(?,?,?,?,?,?,?,?,NULL)',
                            (uuid.uuid4().hex, mid, student['id'], json.dumps(item, ensure_ascii=False), draft_for(student, item), 'pending_review', now(), now()))
                    self.store.db.execute('UPDATE messages SET extracted=1 WHERE id=?', (mid,))
                    self.store.db.commit()
                except Exception:
                    self.store.db.rollback()
                    raise
            created += len(items)
        return created

    async def run(self, sid, jid):
        context = None
        browser = None
        try:
            async with self.limit:
                student = self.store.student(sid)
                self.update(jid, 'logging_in', '正在打开邮箱，请保留浏览器窗口')
                async with async_playwright() as pw:
                    browser = await pw.chromium.launch(headless=self.headless)
                    state_file = self.store.root / (sid + '.state')
                    state = self.store.unseal(state_file.read_bytes()) if state_file.exists() else None
                    context = await browser.new_context(storage_state=state)
                    allowed = student['config']['allowed_hosts']
                    async def route_guard(route):
                        req = route.request
                        if req.is_navigation_request() and req.frame == page.main_frame and urlparse(req.url).hostname not in allowed:
                            self.blocked[jid] = urlparse(req.url).hostname or '未知域名'
                            await route.abort()
                        else:
                            await route.continue_()
                    page = await context.new_page()
                    await context.route('**/*', route_guard)
                    try:
                        await self.login(page, student, jid)
                        state_file.write_bytes(self.store.seal(await context.storage_state(indexed_db=True)))
                        os.chmod(state_file, 0o600)
                        self.update(jid, 'collecting', '登录成功，正在读取邮件并提取事项')
                        messages = await self.collect(page, student)
                        count = await self.process(student, messages)
                        self.update(jid, 'completed', f'检查本次可见的 {len(messages)} 封邮件，新增 {count} 个待审核事项；不代表全邮箱历史扫描')
                    except Exception as exc:
                        await self.diagnose(page, student, jid, exc)
                        raise
        except asyncio.CancelledError:
            self.update(jid, 'cancelled', '任务已取消')
            raise
        except Exception as exc:
            # No raw browser/HTTP errors: they may contain identifiers, URLs or tokens.
            safe = self.failure_detail(exc, jid)
            self.update(jid, 'failed', safe[:300])
        finally:
            self.views.pop(jid, None)
            self.blocked.pop(jid, None)
            with suppress(BrowserError):
                if context:
                    await context.close()
                if browser:
                    await browser.close()

    async def stop(self):
        for task in self.tasks.values():
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)
