import os
import anyio
import httpx
from dotenv import load_dotenv
from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, TextBlock, query

load_dotenv()

# Linear Functions
ISSUE_DETAIL_QUERY = """
query IssueContext($id: String!) {
  issue(id: $id) {
    id
    identifier
    title
    description
    url
 
    # Current status, so the model's suggested status starts from where the issue is now
    state {
      name
      type        # backlog | unstarted | started | completed | canceled
    }
 
    # Parent issue, if this is a sub-issue. Gives the model the larger goal.
    parent {
      identifier
      title
    }
 
    # Comments come back in createdAt order, but the API has no sort direction,
    # so sort client-side to be safe.
    comments(first: 100) {
      nodes {
        id
        body
        createdAt
        user {
          name
          isMe      # true for comments posted with your API key
        }
        parent {
          id        # non-null means it's a reply in a thread
        }
      }
      pageInfo {
        hasNextPage
        endCursor
      }
    }
  }
}
"""


async def get_issue(issue_identifier: str):
    async with httpx.AsyncClient(
        headers={"Authorization": str(os.getenv("LINEAR_API_KEY"))},
        verify=False,
    ) as client:
        result = await client.post(
            "https://api.linear.app/graphql",
            json={"query": ISSUE_DETAIL_QUERY, "variables": {"id": "BEI-22"}},
        )
        response = result.json()
        print(response)


async def get_langfuse_trace(session_id: str):
    pass


options = ClaudeAgentOptions(
    cwd="C:/Users/ar14152/code/cpc-agentic-workplace-platform",
    mcp_servers={
        "langfuse": {
            "type": "http",
            "url": "http://10.60.227.122:3000/api/public/mcp",
            "headers": {
                "Authorization": f"Basic {str(os.getenv("LANGFUSE_ENCODED_TOKEN"))}"
            },
        }
    },
)


async def _run():
    # async for message in query(prompt="find the latest trace for the following session: 11c9970b-7dd3-4bf4-9b1f-97c5d851e90b", options=options):
    #     if isinstance(message, AssistantMessage):
    #         for block in message.content:
    #             if isinstance(block, TextBlock):
    #                 print(block.text)
    issue = await get_issue("BEI-22")


def main() -> None:
    anyio.run(_run)


if __name__ == "__main__":
    main()
