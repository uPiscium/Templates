# Test fixtures

`agent-knowledge-vault-19-baseline.pack` is a minimal Git pack containing the
commit, tree, and blobs reachable from the AgentKnowledgeVault Task #19
baseline tree at
`1e3a795d5e2717f9c670a812777c4a38c9592db0`. It deliberately omits history
outside that snapshot. Tests import it into a temporary repository so the
source-side provenance recovery runs against the authoritative baseline without
network access.

Expected SHA-256:

```text
e4bb9e9240d40543404bdb094446104ec7984f8cb72a6bcce7d042dc3e670bab
```

The fixture contains only files already tracked by
`upiscium/AgentKnowledgeVault` at that public commit; ignored Task State and
credentials are not included. The test constructs the registered Task
worktree and active receipt/authority pair locally from immutable Templates Git
objects.
