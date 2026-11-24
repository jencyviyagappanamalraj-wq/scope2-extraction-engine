| element | azure | docling | engine_accurate | engine_standard | markitdown | opensource_md | best tool | engine vs best competitor | router |
|---|---|---|---|---|---|---|---|---|---|
| heading | 0.27 | 0.90 | 0.99 | 0.99 | 0.64 | 0.82 | engine_accurate | engine leads by +0.09 (vs docling) | rules |
| text | 0.18 | 0.39 | 0.96 | 0.96 | 0.26 | 0.85 | engine_accurate | engine leads by +0.11 (vs opensource_md) | rules+pypdf |
| table | 0.00 | 0.48 | 0.93 | 0.93 | 0.43 | 0.69 | engine_accurate | engine leads by +0.24 (vs opensource_md) | rules+ai |
| formula | 0.33 | 0.00 | 1.00 | 0.00 | 0.00 | 0.00 | engine_accurate | engine leads by +0.67 (vs azure) | rules+ai |
| key_value | 0.58 | 0.18 | 1.00 | 1.00 | 0.47 | 0.98 | engine_accurate | engine leads by +0.02 (vs opensource_md) | rules+ai |
| image | 0.09 | 1.00 | 1.00 | 1.00 | 0.91 | 0.98 | docling | engine leads by +0.00 (vs docling) | rules |

Mean seconds per document: azure 27.6s, docling 298.1s, engine_accurate 5.9s, engine_standard 4.1s, markitdown 16.6s, opensource_md 5.7s, pdfplumber_text 2.7s, pypdf_text 1.8s
