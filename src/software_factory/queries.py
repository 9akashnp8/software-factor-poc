ISSUE_BY_LABEL_QUERY = """
query TodoIssues($statusName: String!, $labelName: String!, $after: String) {
  issues(
    filter: {
      state: { name: { eq: $statusName } }
      labels: { name: { eq: $labelName } }
    }
    orderBy: createdAt
    first: 50
    after: $after
  ) {
    nodes {
      id
      identifier
      title
      createdAt
      state { name type }
      assignee { name }
    }
    pageInfo {
      hasNextPage
      endCursor
    }
  }
}
"""

ISSUE_DETAIL_QUERY = """
query IssueContext($id: String!) {
  issue(id: $id) {
    id
    identifier
    title
    description
    url
 
    labels {
      nodes {
        name
      }
    }

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
  }
}
"""

ISSUE_STATES_QUERY = """
query IssueTeamStates($id: String!) {
  issue(id: $id) {
    team {
      states {
        nodes {
          id
          name
        }
      }
    }
  }
}
"""

SET_STATE_MUTATION = """
mutation SetIssueState($id: String!, $stateId: String!) {
  issueUpdate(id: $id, input: { stateId: $stateId }) {
    success
  }
}
"""
