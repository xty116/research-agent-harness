---
name: dataset-profiling
description: Inspect an available local CSV, TSV or JSON dataset sample before a data-reserve recommendation.
allowed-tools: profile_dataset read_document find_in_document
---
Use only a sample already placed inside data/samples. Call profile_dataset for schema, missingness and duplicates; provide known ID and split fields for overlap checks. Report scanned rows, prefix sampling and truncation. A sample profile does not establish whole-dataset quality, legal availability, bias or fitness for training. Link the aggregate artifact and list untested dimensions.
