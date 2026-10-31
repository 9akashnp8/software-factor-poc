import httpx

from software_factory.trace_cleaner import MAIN_TRACE_NAMES, select_main_trace_ids


async def fetch_session(client: httpx.AsyncClient, session_id: str) -> dict:
    result = await client.get(f"/api/public/sessions/{session_id}")
    result.raise_for_status()
    return result.json()


async def fetch_trace(client: httpx.AsyncClient, trace_id: str) -> dict:
    result = await client.get(f"/api/public/traces/{trace_id}")
    result.raise_for_status()
    return result.json()


async def fetch_conversation(
    session_id: str,
    base_url: str,
    encoded_token: str,
    main_trace_names: frozenset[str] = MAIN_TRACE_NAMES,
) -> tuple[dict, list[dict]]:
    """Fetch a session and the full trace (with observations) of each of its chat turns."""
    async with httpx.AsyncClient(
        base_url=base_url,
        headers={"Authorization": f"Basic {encoded_token}"},
        timeout=30,
        verify=False
    ) as client:
        session = await fetch_session(client, session_id)
        # The sessions endpoint lists traces without observations, so fetch each turn
        traces = [
            await fetch_trace(client, trace_id)
            for trace_id in select_main_trace_ids(session, main_trace_names)
        ]
    return session, traces
