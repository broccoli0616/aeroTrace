import os

# Keep the test suite offline and deterministic: BM25 only, whatever .env says.
# (Set before aerotrace is imported; the .env loader never overrides existing variables.)
os.environ["AEROTRACE_EMBEDDINGS"] = ""
