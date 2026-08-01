import asyncio
from collections import defaultdict


class SSEManager:
    def __init__(self):
        self._subscribers: dict[str, set[asyncio.Queue]] = defaultdict(set)
        self._history: dict[str, list[dict]] = defaultdict(list)

    async def emit(self, task_id: str, event: str, data: dict):
        payload = {"event": event, "data": data}
        self._history[task_id].append(payload)
        self._history[task_id] = self._history[task_id][-100:]
        for queue in list(self._subscribers[task_id]):
            await queue.put(payload)

    def subscribe(self, task_id: str):
        queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._subscribers[task_id].add(queue)
        return queue

    def unsubscribe(self, task_id: str, queue: asyncio.Queue):
        self._subscribers[task_id].discard(queue)

    def history(self, task_id: str):
        return list(self._history.get(task_id, []))

    def clear(self, task_id: str):
        self._history.pop(task_id, None)


sse_manager = SSEManager()
