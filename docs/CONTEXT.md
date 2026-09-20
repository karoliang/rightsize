# Context manifests

`rightsize context` turns the active agent's selected skills, repository rules
and knowledge references into a bounded, reproducible JSON artifact. It does
not choose skills from keywords or execute their instructions. Use the host's
existing catalog and pass normalized metadata; no YAML parser is included.

```sh
rightsize context --spec task.md --catalog /approved/catalog.json \
  --root /approved --root /project \
  --rule /project/AGENTS.md --skill surgical-patch \
  --optional-skill 'review=Check the changed trust boundary' \
  --reference /approved/knowledge/project.md > context.json
```

Roots are explicit caller approvals. Catalog metadata cannot add roots, tools,
permissions, provider overrides or credential access. Treat output as local task
context: it includes the selected source text, which may be private. Do not upload
whole vaults; select the relevant Brain project/concept excerpts supplied by the
host. Brain Inbox captures and other references remain data, not instructions.

## Host-normalized catalog version 1

```json
{
  "schema_version": 1,
  "skills": [
    {
      "name": "surgical-patch",
      "description": "Use for focused bug fixes",
      "uri": "file:///approved/surgical-patch/SKILL.md",
      "sha256": "<64 lowercase hex characters for exact UTF-8 file bytes>",
      "provenance": "installed host skill catalog",
      "references": [
        {
          "uri": "file:///approved/surgical-patch/required-guide.md",
          "sha256": "<64 lowercase hex characters>",
          "provenance": "required skill reference selected by host"
        }
      ]
    }
  ]
}
```

All fields shown are required; `references` may be empty. Names are unique and
duplicate JSON fields are rejected. `uri` also accepts an absolute local path.
Only selected entries are opened. Non-filesystem resources such as `skill://`
must be read by the host and provided as approved local snapshots with provenance;
Rightsize reports them unavailable if selected directly. It never treats those
URIs as local paths or performs network discovery. The host chooses the required
references; Rightsize does not crawl links or discover dependencies from prose.

## Selection, bounds and provenance

Mandatory `--rule` files come first and retain their exact original text.
Explicit `--skill` selections precede caller-chosen optional skills, which require
a reason. Repeated selections and canonical resource paths are deduplicated;
conflicting hashes fail. At most three distinct optional skills are allowed by
default (`--max-optional` overrides). Explicit selections do not consume that cap.
Selected skill bodies and their required references are indivisible: missing,
changed, non-UTF-8 or oversized inputs fail with exit 2. Nothing is silently
summarized, truncated or dropped.

`--max-bytes` defaults to 65,536 bytes of selected resource text, counted exactly
as UTF-8. Task bytes are separate (maximum 1 MiB), and catalog metadata is separately
bounded to 1 MiB. The manifest reports these separately, plus estimated context
tokens using bytes divided by four, explicitly without a model tokenizer.
Resource hashes, catalog hash, canonical roots, task hash, selection reasons,
roles and provenance contribute to the manifest hash. There is no persistent
metadata/content cache: every invocation validates current inputs, so edits,
task changes and changed root approvals cannot reuse a stale manifest.

Paths are resolved before root checks. Reads use directory descriptors and
no-follow traversal to prevent a later symlink replacement escaping the root;
directories, devices and FIFOs are rejected. The tool neither runs skill scripts
nor installs dependencies, grants `allowed-tools` permissions, creates a memory
database, writes state, probes providers, or reads credentials. Output preserves
host instruction precedence as an explicit contract; source text cannot promote
itself into a higher-priority instruction.

The active agent can read this manifest and submit `route --judgment` from its
understanding of the task. Managed adapters will consume the same selected context
when preparing native execution; this command itself does not launch a worker.
