from textractor import Textractor
from textractor.data.constants import TextractFeatures

extractor = Textractor()  # uses the credentials from aws configure

document = extractor.analyze_document(
    file_source="sample.png",
    features=[TextractFeatures.LAYOUT, TextractFeatures.TABLES],
)

with open("output.md", "w", encoding="utf-8") as f:
    f.write(document.to_markdown())

print("Done! Check output.md")