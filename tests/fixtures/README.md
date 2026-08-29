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

`agent-knowledge-vault-19-templates-source.pack` contains the two exact
Templates source commits from the stranded receipt scenario, together with the
trees and blobs required to materialize their checkouts:

```text
receipt source: 076653b054f5d8cbce4a28bcb6b381e9f30ee669
expected source: 835203b6f1ae342d31ed74372728e9862b9b36f0
SHA-256: 54e7d83478d67499c5afd522a3fc27766048ce669c7169c9e8bc8cb2a04fe5cd
```

The test imports this pack into a new temporary Templates repository before
resolving either revision. It does not share the developer checkout object
database, fetch a deleted pull-request ref, or use the network. Parent history
outside the two snapshots is deliberately omitted.
