import os
import sys
import asyncio
import uvicorn

if __name__ == '__main__':
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    uvicorn.run('app.main:create_app', factory=True, host='127.0.0.1', port=int(os.getenv('PORT', '8000')), workers=1, loop='asyncio')
