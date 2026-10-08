import asyncio
from typing import Dict, List, Optional, Set


class MockRedis:
    def __init__(self):
        self.lists: Dict[str, List[str]] = {}
        self.sets: Dict[str, Set[str]] = {}
        self.strings: Dict[str, str] = {}
        self.lock = asyncio.Lock()

    async def rpush(self, key: str, *values: str) -> int:
        async with self.lock:
            if key not in self.lists:
                self.lists[key] = []
            for v in values:
                self.lists[key].append(str(v))
            return len(self.lists[key])

    async def lpop(self, key: str) -> Optional[str]:
        async with self.lock:
            if key in self.lists and self.lists[key]:
                return self.lists[key].pop(0)
            return None

    async def lrange(self, key: str, start: int, stop: int) -> List[str]:
        async with self.lock:
            l = self.lists.get(key, [])
            if stop == -1:
                return list(l[start:])
            return list(l[start : stop + 1])

    async def llen(self, key: str) -> int:
        async with self.lock:
            return len(self.lists.get(key, []))

    async def lrem(self, key: str, count: int, value: str) -> int:
        async with self.lock:
            if key not in self.lists:
                return 0
            orig_len = len(self.lists[key])
            self.lists[key] = [x for x in self.lists[key] if x != value]
            return orig_len - len(self.lists[key])

    async def sadd(self, key: str, *members: str) -> int:
        async with self.lock:
            if key not in self.sets:
                self.sets[key] = set()
            added = 0
            for m in members:
                if str(m) not in self.sets[key]:
                    self.sets[key].add(str(m))
                    added += 1
            return added

    async def srem(self, key: str, *members: str) -> int:
        async with self.lock:
            if key not in self.sets:
                return 0
            removed = 0
            for m in members:
                if str(m) in self.sets[key]:
                    self.sets[key].remove(str(m))
                    removed += 1
            return removed

    async def scard(self, key: str) -> int:
        async with self.lock:
            return len(self.sets.get(key, set()))

    async def smembers(self, key: str) -> Set[str]:
        async with self.lock:
            return set(self.sets.get(key, set()))

    async def get(self, key: str) -> Optional[str]:
        async with self.lock:
            return self.strings.get(key)

    async def set(
        self, key: str, value: str, nx: bool = False, ex: Optional[int] = None
    ) -> bool:
        async with self.lock:
            if nx and key in self.strings:
                return False
            self.strings[key] = str(value)
            if ex is not None:
                if not hasattr(self, "ttls"):
                    self.ttls = {}
                self.ttls[key] = int(ex)
            return True

    async def ttl(self, key: str) -> int:
        async with self.lock:
            if not hasattr(self, "ttls"):
                self.ttls = {}
            if key in self.ttls:
                return self.ttls[key]
            if key in self.strings or key in self.lists or key in self.sets:
                return -1
            return -2

    async def delete(self, *keys: str) -> int:
        async with self.lock:
            if not hasattr(self, "ttls"):
                self.ttls = {}
            deleted = 0
            for k in keys:
                self.ttls.pop(k, None)
                if k in self.strings:
                    del self.strings[k]
                    deleted += 1
                if k in self.lists:
                    del self.lists[k]
                    deleted += 1
                if k in self.sets:
                    del self.sets[k]
                    deleted += 1
            return deleted

    async def eval(self, script: str, numkeys: int, *args) -> Optional[str]:
        """Executes the atomic Lua slot acquisition script atomically."""
        async with self.lock:
            # Keys: 1: queue_key, 2: active_key, 3: pause_key
            queue_key = args[0]
            active_key = args[1]
            pause_key = args[2]
            max_active = int(args[3])

            is_paused = self.strings.get(pause_key) == "1"
            if is_paused:
                return None

            active_set = self.sets.get(active_key, set())
            if len(active_set) >= max_active:
                return None

            queue_list = self.lists.get(queue_key, [])
            if queue_list:
                job_id = queue_list.pop(0)
                if active_key not in self.sets:
                    self.sets[active_key] = set()
                self.sets[active_key].add(job_id)
                return job_id

            return None

    async def publish(self, channel: str, message: str) -> int:
        return 1

    def pubsub(self):
        class MockPubSub:
            async def subscribe(self, *channels):
                pass
            async def listen(self):
                if False:
                    yield {}
        return MockPubSub()
