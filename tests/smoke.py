"""Against a running isolated server: CHANNEL_URL and CHANNEL_KEY_FILE required."""
import asyncio
import json
import os
import uuid
from pathlib import Path

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus


async def main():
    url = os.environ['CHANNEL_URL'].rstrip('/')
    headers = {'Authorization': 'Bearer ' + Path(os.environ['CHANNEL_KEY_FILE']).read_text().strip()}
    channel = 'smoke-' + uuid.uuid4().hex
    path = url + '/channels/' + channel
    async with httpx.AsyncClient(timeout=70) as http_client:
        assert (await http_client.get(url + '/health')).status_code == 200
        assert (await http_client.get(path + '/messages')).status_code == 401
        assert (await http_client.get(path + '/messages', headers={'Authorization': 'Bearer wrong'})).status_code == 401
        try:
            async with connect(path.replace('http', 'ws', 1) + '/subscribe'):
                raise AssertionError('unauthenticated websocket accepted')
        except InvalidStatus as exc:
            assert exc.response.status_code == 403
        sockets = [await connect(path.replace('http', 'ws', 1) + '/subscribe?cursor=0-0', additional_headers=headers, max_size=16*1024*1024) for _ in range(3)]
        try:
            text = '긴 문맥🙂' * 100000
            assert len(text.encode()) > 1024*1024
            response = await http_client.post(path + '/messages', headers=headers, json={'session_id': 'smoke', 'text': text})
            response.raise_for_status()
            first = response.json()
            events = await asyncio.gather(*(asyncio.wait_for(websocket.recv(), 10) for websocket in sockets))
            assert all(json.loads(event_payload)['text'] == text for event_payload in events)
            response = await http_client.post(path + '/messages', headers=headers, json={'session_id': 'smoke', 'text': 'reply', 'reply_to': first['id']})
            reply = response.json()
            assert json.loads(await asyncio.wait_for(sockets[0].recv(), 10))['reply_to'] == first['id']
            replay = (await http_client.get(path + '/messages', headers=headers, params={'cursor': first['id']})).json()
            assert replay['messages'][0]['id'] == reply['id']
            invalid = await http_client.get(path + '/messages', headers=headers, params={'cursor': 'bad'})
            assert invalid.status_code == 400
            oversized = await http_client.post(path + '/messages', headers=headers, json={'session_id': 'smoke', 'text': 'x' * (8*1024*1024+1)})
            assert oversized.status_code in (400, 413)
        finally:
            await asyncio.gather(*(websocket.close() for websocket in sockets))
    async with (streamablehttp_client(url + '/mcp', headers=headers) as (read, write, _),
                ClientSession(read, write) as session):
        await session.initialize()
        assert {'publish', 'receive'} <= {tool.name for tool in (await session.list_tools()).tools}
        result = await session.call_tool('publish', {'channel': channel, 'text': 'sdk', 'session_id': 'smoke'})
        assert not result.isError
        result = await session.call_tool('receive', {'channel': channel, 'cursor': reply['id']})
        assert not result.isError
    print('PASS: auth HTTP/WS, 3 subscribers, >1 MiB Unicode, reply, cursor replay, validation, oversize, official MCP initialize/list/call')


asyncio.run(main())
