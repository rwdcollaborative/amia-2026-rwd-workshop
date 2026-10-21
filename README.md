# amia-2026-rwd-workshop

| Folder | What's in it |
|--------|--------------|
| `staging/` | Loads CMS DE-SynPUF and the OMOP vocabulary into BigQuery (`synpuf_omop`), checks what's there (`diagnostics.py`), and builds the workshop dataset (`augment.py` → `synpuf_workshop`). |
| `notebooks/` | Participant notebooks: a guided OMOP demo and the survival and A1C exercises. |
| `atlas/` | A self-hosted OHDSI ATLAS vocabulary browser. |
| `docs/` | Design notes, plus [what we learned about the SynPUF data](docs/synpuf-data-findings.md). |

Setup order on a fresh workspace: `staging/load_synpuf.sh load`, then
`staging/augment.py`, then open the notebooks. See `staging/README.md`.
