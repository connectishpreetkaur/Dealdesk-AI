"""
DealDesk AI — Day 2: RAG Knowledge Base Builder
Uses gemini_client with automatic fallback.
Run ONCE: python knowledge_base/build_rag.py
"""

import os, sys, time, json
import fitz
from pinecone import Pinecone, ServerlessSpec
from dotenv import load_dotenv

# Add parent dir so we can import gemini_client
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from gemini_client import gemini_embed

load_dotenv()

GREEN="\033[92m"; YELLOW="\033[93m"; RED="\033[91m"; BOLD="\033[1m"; RESET="\033[0m"
def ok(m):     print(f"{GREEN}  ✓  {m}{RESET}")
def warn(m):   print(f"{YELLOW}  ⚠  {m}{RESET}")
def err(m):    print(f"{RED}  ✗  {m}{RESET}")
def header(m): print(f"\n{BOLD}{m}{RESET}\n" + "─"*50)

CHUNK_SIZE   = 500
CHUNK_OVERLAP= 50
BATCH_SIZE   = 50
DIMENSION    = 384   # all-MiniLM-L6-v2 (sentence-transformers)

BOOKS = [
    {"filename":"geltner_miller.pdf","title":"Commercial Real Estate Analysis and Investments",
     "author":"Geltner & Miller","focus":["valuation","DCF","cap_rate","market_cycles","NOI"]},
    {"filename":"linneman.pdf","title":"Real Estate Finance and Investments",
     "author":"Peter Linneman","focus":["financing","deal_structure","investment_thesis","returns"]},
    {"filename":"gallinelli.pdf","title":"What Every Real Estate Investor Needs to Know About Cash Flow",
     "author":"Frank Gallinelli","focus":["cash_flow","DSCR","IRR","NOI","financial_metrics"]},
]

def extract_pdf_text(path):
    doc  = fitz.open(path)
    text = "\n\n".join(f"[PAGE {i+1}]\n{p.get_text()}" for i,p in enumerate(doc) if p.get_text().strip())
    doc.close()
    return text

def chunk_text(text, size=CHUNK_SIZE, overlap=CHUNK_OVERLAP):
    words  = text.split()
    chunks = []
    start  = 0
    while start < len(words):
        end   = min(start + size, len(words))
        chunk = " ".join(words[start:end])
        if len(chunk.strip()) > 100:
            chunks.append(chunk)
        start += size - overlap
    return chunks

def embed_chunks_batched(chunks):
    """Embed chunks in small batches with fallback."""
    embeddings = []
    batch_size = 10
    for i in range(0, len(chunks), batch_size):
        batch = chunks[i:i+batch_size]
        embs  = gemini_embed(batch, task_type="RETRIEVAL_DOCUMENT")
        embeddings.extend(embs)
        time.sleep(0.5)
        if (i // batch_size) % 5 == 0:
            print(f"    Embedded {min(i+batch_size, len(chunks))}/{len(chunks)} chunks...")
    return embeddings

def upsert_to_pinecone(index, vectors, batch_size=BATCH_SIZE):
    for i in range(0, len(vectors), batch_size):
        batch = vectors[i:i+batch_size]
        index.upsert(vectors=batch)
        print(f"    Upserted {min(i+batch_size, len(vectors))}/{len(vectors)} vectors...")
        time.sleep(0.2)

def main():
    header("DEALDESK AI — DAY 2: BUILDING RAG KNOWLEDGE BASE")

    pinecone_key = os.getenv("PINECONE_API_KEY")
    index_name   = os.getenv("PINECONE_INDEX_NAME","dealdesk-knowledge")

    if not os.getenv("GEMINI_API_KEY"): err("GEMINI_API_KEY missing"); sys.exit(1)
    if not pinecone_key:                err("PINECONE_API_KEY missing"); sys.exit(1)
    ok("API keys loaded")

    pc = Pinecone(api_key=pinecone_key)

    header("STEP 1 — Setting up Pinecone index")
    existing = [idx.name for idx in pc.list_indexes()]
    if index_name in existing:
        warn(f"Index '{index_name}' already exists — skipping creation")
    else:
        pc.create_index(name=index_name, dimension=DIMENSION, metric="cosine",
                        spec=ServerlessSpec(cloud="aws", region="us-east-1"))
        time.sleep(5)
        ok(f"Index '{index_name}' created")

    index = pc.Index(index_name)
    ok(f"Index ready — {index.describe_index_stats()['total_vector_count']} vectors stored")

    books_dir = "data/books"
    total     = 0

    for book in BOOKS:
        header(f"STEP 2 — Processing: {book['author']}")
        pdf_path = os.path.join(books_dir, book["filename"])
        if not os.path.exists(pdf_path):
            warn(f"Not found: {pdf_path} — skipping"); continue

        print("  Extracting text...")
        text   = extract_pdf_text(pdf_path)
        ok(f"Extracted {len(text):,} characters")

        print("  Chunking...")
        chunks = chunk_text(text)
        ok(f"Created {len(chunks)} chunks")

        print("  Embedding (auto-fallback enabled)...")
        print("  Takes 2–5 mins per book — do not close terminal")
        embeddings = embed_chunks_batched(chunks)
        ok(f"Embedded {len(embeddings)} chunks")

        vectors = []
        for i,(chunk,emb) in enumerate(zip(chunks,embeddings)):
            page = "unknown"
            if "[PAGE " in chunk:
                try: page = chunk.split("[PAGE ")[1].split("]")[0]
                except: pass
            vectors.append({
                "id":     f"{book['author'].replace(' ','_').replace('&','and')}_{i:05d}",
                "values": emb,
                "metadata": {
                    "text":   chunk[:1000],
                    "author": book["author"],
                    "title":  book["title"],
                    "focus":  ",".join(book["focus"]),
                    "page":   page
                }
            })

        print("  Uploading to Pinecone...")
        upsert_to_pinecone(index, vectors)
        total += len(vectors)
        ok(f"{book['author']} — {len(vectors)} vectors stored")

    header("STEP 3 — Testing retrieval")
    from gemini_client import gemini_embed as ge
    test_queries = [
        "What is a healthy DSCR threshold for CRE loans?",
        "How do you calculate cap rate?",
        "Key risk factors in multifamily underwriting"
    ]
    for q in test_queries:
        print(f"\n  Query: \"{q}\"")
        qv      = ge(q, task_type="RETRIEVAL_QUERY")
        matches = index.query(vector=qv, top_k=2, include_metadata=True)["matches"]
        for m in matches:
            print(f"  {GREEN}[{m['metadata']['author']} | score: {round(m['score'],3)}]{RESET}")
            print(f"  {m['metadata']['text'][:200]}...")

    os.makedirs("outputs", exist_ok=True)
    with open("outputs/rag_build_summary.json","w") as f:
        json.dump({"total_vectors":total,"index":index_name,
                   "books":[b["author"] for b in BOOKS]},f,indent=2)

    header("DAY 2 COMPLETE")
    ok(f"{total} total vectors stored in Pinecone")
    ok("Geltner + Linneman + Gallinelli are now your agents brain")
    ok("Summary saved to outputs/rag_build_summary.json")
    print(f"\n  {BOLD}Next:{RESET} python agents/run_pipeline.py data/sample_oms/your_om.pdf\n")

if __name__ == "__main__":
    main()
