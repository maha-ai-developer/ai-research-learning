# Inventory artifacts

This folder preserves earlier inventory outputs so they are available in the project rather than only in chat attachments.

- `ai_research_source_inventory.xlsx`: updated source inventory workbook.
- `ai_research_source_inventory_initial.xlsx`: original workbook snapshot.
- `ai_research_seed_urls.csv` and `.json`: structured initial URL/source manifest exports.
- `ai_research_collection_plan.csv`: early collection plan.
- `ai_research_inventory_early.sqlite`: earlier inventory database snapshot.
- `ai_research_metadata_inventory_prepared.sqlite`: metadata collector's prepared schema and source URL inventory at the time of export.

The SQLite files are historical snapshots. The current collector creates a separate ignored runtime database at `data/runtime/inventory.sqlite`.

Counts in these files describe the inventory or URL manifest. They do not mean the full pages, papers, videos, or transcripts have been downloaded.
