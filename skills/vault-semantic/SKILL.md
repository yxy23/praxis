---
name: vault-semantic
description: "Semantic similarity retrieval via Smart Connections plugin — third leg of the retrieval stool alongside /vault-graph (structural) and flat keyword search (lexical). Pipes through pre-computed vault embeddings to find notes with content semantically similar to a query, including notes that match neither wikilinks nor keywords."
---

# Vault-Semantic — Semantic Similarity Retrieval

Third leg of the retrieval stool, complementing `/vault-graph` (structural) and flat keyword search (lexical). Pipes through the Smart Connections plugin's pre-computed vault embeddings (model: `TaylorAI/bge-micro-v2`, 384-dim) to find notes whose content is semantically similar to a query — including notes that match neither wikilinks nor keywords.

Use `/vault-semantic <query>` directly or as semantic retrieval support for `/research-survey`.

## Preconditions and constraints

### Preconditions

- Obsidian Desktop is running with the vault open
- Smart Connections plugin enabled and indexed (check `.smart-env/multi/` has `.ajson` files)
- `obsidian` CLI on PATH (typically `/usr/local/bin/obsidian`)

### Constraints

- never fabricate matches — only entries returned by Smart Connections lookup
- if Smart Connections is not loaded or index is empty, abort with clear message (do not fall back to graph or grep — those are separate skills)
- cap output to top 30 by default; user can request `top=N` for deeper exploration
- skill is read-only — no edits to vault content

## When to Use Over Graph or Flat

| Situation | Best primitive |
|---|---|
| Query phrases match user's named constructs (e.g., "endogenous utility") | `/vault-graph` (graph traversal from seed) |
| Query is a literal phrase that should appear in target notes | flat `search_vault_simple` |
| Query is a *concept* the user may have described in different words | `/vault-semantic` |
| Need every note touching a topic regardless of wikilink density | All three, merged |

Semantic-only hits are the highest-value novel finding: notes wikilinked nowhere relevant, no keyword match, but content cosine-similar to the query. These are the cross-domain analogies the user's curation hasn't yet reached.

## How to Use

| Mode | Invocation | Behavior |
|---|---|---|
| Default | `/vault-semantic <query>` | Top-30 semantic neighbors |
| Top-N | `/vault-semantic <query> top=N` | Top-N |
| File-level | `/vault-semantic <query> dedupe=file` | Collapse section-level chunks to file-level (one entry per file, max score) |
| Threshold | `/vault-semantic <query> min=0.7` | Drop results below cosine threshold |

Smart Connections returns section-level granularity by default (e.g., `papers/foo.md#Heading#Subheading`). Use `dedupe=file` for cleaner file-list output; default keeps sections for precision.

## Core Workflow

### Phase 0 — Pre-flight

1. Check Obsidian Desktop is running: `obsidian vault info=name` returns non-empty.
2. Check Smart Connections is loaded: query plugin presence via eval.
3. Check index isn't empty: confirm `.smart-env/multi/` has files (≥ 100 expected for any active vault).
4. Check the Smart Environment collections are ready. Smart Connections 4.x may
   leave the environment at `state: init` until a view or API consumer loads it.
   Plugin presence alone is therefore insufficient.

If any check fails, abort with diagnostic message. Do not silently degrade.

#### Smart Connections 4.x lazy-load gate

Resolve the active vault name once and use it on every CLI call. If
`smart_sources` and `lookup_lists` are not ready, start `env.load()` inside a
non-Promise wrapper so `obsidian eval` returns immediately, then poll for the
collections. Loading the environment is read-only with respect to vault notes;
it opens the existing `.smart-env` index.

```bash
VAULT_NAME=$(obsidian vault info=name)

# Probe
ready=$(obsidian vault="$VAULT_NAME" eval code='{
  const p=app.plugins.plugins["smart-connections"];
  p?.env?.smart_sources && p?.env?.lookup_lists ? "READY" :
  p ? "NOT_READY" : "PLUGIN_MISSING"
}')

case "$ready" in
  *PLUGIN_MISSING*) echo "Smart Connections plugin is not loaded." >&2; exit 1 ;;
  *NOT_READY*)
    obsidian vault="$VAULT_NAME" eval code='(()=>{
      const p=app.plugins.plugins["smart-connections"];
      window._vs_load="PENDING";
      Promise.resolve(p.env.load())
        .then(()=>{window._vs_load="READY"})
        .catch(e=>{window._vs_load="ERROR: "+e.message});
      return "LOAD_STARTED";
    })()' >/dev/null
    for i in {1..30}; do
      ready=$(obsidian vault="$VAULT_NAME" eval code='{
        const e=app.plugins.plugins["smart-connections"]?.env;
        e?.smart_sources && e?.lookup_lists ? "READY" :
        (typeof window._vs_load === "string" ? window._vs_load : "PENDING")
      }')
      case "$ready" in
        *READY*) break ;;
        *ERROR*) echo "$ready" >&2; exit 1 ;;
        *) sleep 1 ;;
      esac
    done
    ;;
esac

case "$ready" in
  *READY*) ;;
  *) echo "Smart Connections environment did not become ready within 30s." >&2; exit 1 ;;
esac
```

### Phase 1 — Async lookup

Use Smart Connections 4.x's `lookup_lists` API. The compatibility branch keeps
the prior `smart_sources.lookup()` API available for older installations. Both
return a Promise, so fire the lookup inside a synchronous wrapper, store the
eventual result on `window`, and poll in a second eval. Never interpolate a raw
query into JavaScript: serialize it as JSON first.

```bash
# QUERY is the natural-language query; TOP is a validated integer, default 30.
QUERY_JSON=$(jq -Rn --arg q "$QUERY" '$q')

# Fire. The wrapper's explicit string return prevents the CLI from waiting on
# the Promise. `smart_blocks` preserves section-level results when available.
obsidian vault="$VAULT_NAME" eval code="(()=>{
  const q=$QUERY_JSON;
  const top=$TOP;
  const env=app.plugins.plugins[\"smart-connections\"].env;
  window._vs=\"PENDING\";
  let promise;
  if (env.lookup_lists?.new_item) {
    const list=env.lookup_lists.new_item({query:q});
    const results_collection_key=env.smart_blocks ? \"smart_blocks\" : \"smart_sources\";
    promise=list.get_results({query:q,results_collection_key,limit:top});
  } else if (env.smart_sources?.lookup) {
    promise=env.smart_sources.lookup({hypotheticals:[q],filter:{limit:top}});
  } else {
    window._vs=\"ERROR: no supported Smart Connections lookup API\";
    return \"ERROR\";
  }
  Promise.resolve(promise)
    .then(r=>{window._vs=r.slice(0,top).map(x=>({path:x.item?.path||x.item?.key||x.path,score:x.score}))})
    .catch(e=>{window._vs=\"ERROR: \"+e.message});
  return \"FIRED\";
})()" >/dev/null

# Poll (up to ~10 seconds)
for i in 1 2 3 4 5 6 7 8 9 10; do
  result=$(obsidian vault="$VAULT_NAME" eval code='typeof window._vs === "string" ? window._vs : JSON.stringify(window._vs)')
  case "$result" in
    *PENDING*) sleep 1 ;;
    *ERROR*) echo "$result" >&2; exit 1 ;;
    *) echo "$result"; break ;;
  esac
done
```

Lookup latency: typically 200ms–1s for small/medium vaults; up to ~3s on large vaults (1000+ sources). The poll loop should resolve in ≤ 2 iterations in normal operation.

### Phase 2 — Parse + rank

Smart Connections returns results pre-sorted by cosine descending. Score range is 0–1; in practice useful matches are 0.55+, strong matches 0.7+, very strong 0.8+.

Path format: `<vault-relative-path>.md[#H1[#H2[#{N}]]]` — the `#{N}` suffix is a chunk-anchor for repeated subheading names.

### Phase 3 — Dedupe (optional)

Default: keep section-level granularity (the user can navigate directly to the relevant heading).

With `dedupe=file`: group by file path (strip `#...` suffix), take max score per file, re-rank.

### Phase 4 — Output

```markdown
# Vault-Semantic — <query>

## Top semantic neighbors
| Rank | Path | Score | Section |
|---|---|---|---|
| 1 | `concepts/<concept-A>.md` | 0.85 | Causal Chain |
| 2 | `projects/<paper-X>.md` | 0.82 | Key Specifications |
| ... | | | |

## Cosine score legend
- 0.8+ : very strong semantic match
- 0.7–0.8 : strong
- 0.55–0.7 : moderate (skim for relevance)
- < 0.55 : weak (default filter — adjust with min=0.X)
```

## Wire-In Behavior (third augmentation leg)

Called from `/research-survey` alongside `/vault-graph` and flat keyword search. Three-way merge:

| Hit pattern | Confidence | Routing |
|---|---|---|
| Graph + Flat + Semantic | Highest | Lead with these as primary anchors |
| Two of three | High | Strong candidates |
| Semantic-only | **Most informative novel signal** | Surface separately as "content-similar but not wikilinked / not keyword-matched — candidate for connection or citation" |
| Graph-only | Curated structure | Standard graph value |
| Flat-only | Possible graph gap | Surface as "needs wiring" candidate per the vault-graph augment rule |

Semantic-only hits are why this primitive matters: they catch cross-domain analogies the user's wikilink structure hasn't reached yet AND that don't match keywords.

## Failure Modes

| Condition | Action |
|---|---|
| Obsidian Desktop not running | Abort with: "Obsidian must be running for `/vault-semantic`. Please open Obsidian." |
| Smart Connections not enabled | Abort with: "Smart Connections plugin is not loaded. Enable it in Obsidian settings." |
| `.smart-env/multi/` empty or missing | Abort with: "Smart Connections index is empty. Trigger reindex in Obsidian: Smart Connections → Reload Sources." |
| Plugin exists but collections are absent | Run the lazy-load gate once; abort if `smart_sources` and `lookup_lists` are still absent after 30s. |
| `window._vs` stays `PENDING` past 10s | Abort with: "Lookup timed out. Check Smart Connections status; index may be rebuilding." |
| Returned cosine scores all < 0.5 | Surface with warning: "No strong semantic matches; consider rephrasing query or check that vault is well-embedded for this topic." |

## CLI Reliability Notes

- `obsidian eval` is the only path to Smart Connections from CLI; no other primitive (search, files, backlinks) exposes semantic similarity.
- Smart Connections 4.x can expose the plugin while leaving its environment at `init`; test collection readiness, not only plugin presence.
- The async-store-poll pattern is required. Always return a non-Promise value from the firing eval; otherwise the CLI can wait indefinitely on the lookup or environment-load Promise.
- Smart Connections internals may change across plugin versions. The preferred path is `env.lookup_lists.new_item(...).get_results(...)`; `env.smart_sources.lookup(...)` is compatibility-only. Verify with a probe query after every plugin upgrade.

## Constraints

- **Never fabricate matches.** Only paths Smart Connections actually returned.
- **Never run from a subagent without verifying Obsidian Desktop is accessible.** Subagents may not inherit GUI access; pre-flight check is non-skippable.
- **Read-only.** No edits to the vault. The skill suggests; the user decides.

