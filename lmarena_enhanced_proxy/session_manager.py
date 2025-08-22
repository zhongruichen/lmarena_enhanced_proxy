import asyncio
import time
from dataclasses import dataclass, field
from typing import List, Dict, Optional
import logging

class SessionStatus:
    AVAILABLE = "available"
    IN_USE = "in_use"
    UNHEALTHY = "unhealthy"

@dataclass
class Session:
    session_id: str
    message_id: str
    model_name: str
    status: str = SessionStatus.AVAILABLE
    creation_time: float = field(default_factory=time.time)
    last_used_time: float = field(default_factory=time.time)

class SessionManager:
    def __init__(self, max_queue_size=100):
        self._session_pools: Dict[str, List[Session]] = {}
        self._locks: Dict[str, asyncio.Lock] = {}
        self._request_queues: Dict[str, asyncio.Queue] = {}
        self._max_queue_size = max_queue_size
        logging.info("SessionManager initialized.")

    async def register_model(self, model_name: str):
        """Registers a new model, preparing a pool, lock, and queue for it."""
        if model_name not in self._session_pools:
            self._session_pools[model_name] = []
            self._locks[model_name] = asyncio.Lock()
            self._request_queues[model_name] = asyncio.Queue(maxsize=self._max_queue_size)
            logging.info(f"Model '{model_name}' registered with SessionManager.")

    async def add_session(self, session: Session):
        """Adds a new, healthy session to the appropriate model's pool."""
        model_name = session.model_name
        if model_name not in self._session_pools:
            await self.register_model(model_name)

        async with self._locks[model_name]:
            self._session_pools[model_name].append(session)
            logging.info(f"Added new session for '{model_name}'. Pool size: {len(self._session_pools[model_name])}")
            # If there are waiting requests, notify one.
            if not self._request_queues[model_name].empty():
                 await self._request_queues[model_name].put(None) # Sentinel value to wake up a waiter

    async def acquire_session(self, model_name: str, timeout: int = 60) -> Optional[Session]:
        """Acquires an available session from the pool for a given model, waiting if necessary."""
        if model_name not in self._locks:
            logging.error(f"Attempted to acquire session for unregistered model '{model_name}'")
            return None

        # First, try to get a session without waiting
        async with self._locks[model_name]:
            for session in self._session_pools.get(model_name, []):
                if session.status == SessionStatus.AVAILABLE:
                    session.status = SessionStatus.IN_USE
                    session.last_used_time = time.time()
                    logging.info(f"Acquired session {session.session_id} for model '{model_name}'")
                    return session

        # If no session was available, wait in the queue
        logging.info(f"No available sessions for '{model_name}', entering wait queue.")
        try:
            # This will raise TimeoutError if the timeout is reached
            await asyncio.wait_for(self._request_queues[model_name].get(), timeout=timeout)

            # When woken up, try to acquire again. This should now succeed.
            async with self._locks[model_name]:
                for session in self._session_pools.get(model_name, []):
                    if session.status == SessionStatus.AVAILABLE:
                        session.status = SessionStatus.IN_USE
                        session.last_used_time = time.time()
                        logging.info(f"Acquired session {session.session_id} for model '{model_name}' after waiting.")
                        return session

            # This should ideally not be reached if logic is correct
            logging.error(f"Woke up for model '{model_name}' but no session was available.")
            return None

        except asyncio.TimeoutError:
            logging.warning(f"Request for model '{model_name}' timed out after waiting {timeout}s in queue.")
            return None

    async def release_session(self, session_id: str):
        """Releases a session and notifies a waiting request if any."""
        for model_name, pool in self._session_pools.items():
            async with self._locks[model_name]:
                for session in pool:
                    if session.session_id == session_id:
                        session.status = SessionStatus.AVAILABLE
                        logging.info(f"Released session {session.session_id} for model '{model_name}'")
                        # If there are waiting requests, notify one.
                        if not self._request_queues[model_name].empty():
                            await self._request_queues[model_name].put(None)
                        return
        logging.warning(f"Could not find session {session_id} to release.")

    async def mark_unhealthy(self, session_id: str):
        """Marks a session as unhealthy and takes it out of rotation."""
        for model_name, pool in self._session_pools.items():
            async with self._locks[model_name]:
                for session in pool:
                    if session.session_id == session_id:
                        session.status = SessionStatus.UNHEALTHY
                        logging.warning(f"Marked session {session.session_id} for model '{model_name}' as UNHEALTHY.")
                        return
        logging.warning(f"Could not find session {session_id} to mark as unhealthy.")

    def get_pool_status(self) -> Dict[str, Dict[str, int]]:
        """Returns the status of all session pools."""
        status = {}
        for model_name, pool in self._session_pools.items():
            status[model_name] = {
                SessionStatus.AVAILABLE: 0,
                SessionStatus.IN_USE: 0,
                SessionStatus.UNHEALTHY: 0,
                "total": len(pool),
                "queue_size": self._request_queues[model_name].qsize() if model_name in self._request_queues else 0
            }
            for session in pool:
                status[model_name][session.status] += 1
        return status
