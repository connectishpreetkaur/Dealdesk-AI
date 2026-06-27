"""
DealDesk AI — Gemini Client with Automatic Fallback & Robust JSON Repair Patch
======================================================================
- Text generation : Gemini 2.5 Flash → 1.5 Flash → 1.5 Flash 8B
- Embeddings      : sentence-transformers (local, FREE, always works)
                    → OpenAI fallback → Gemini fallback

sentence-transformers runs 100% offline — no API key, no quota, no 503s.
"""

import os, time
from google import genai
from google.genai import types
from google.api_core.exceptions import ResourceExhausted
from dotenv import load_dotenv

load_dotenv()

GENERATE_MODELS = [
    "gemini-2.5-flash",           # primary — alive and works great
    "gemini-2.5-flash-lite",      # fallback 1 — cheaper, still alive, same family
    "gemini-3.1-flash-lite",      # fallback 2 — newest budget model, alive June 2026
]

GREEN  = "\033[92m"; YELLOW = "\033[93m"; RED = "\033[91m"; RESET = "\033[0m"

# ── Lazy-load sentence-transformers model (loads once, reused) ────────────────
_ST_MODEL = None
def get_st_model():
    global _ST_MODEL
    if _ST_MODEL is None:
        print(f"{YELLOW}  Loading local embedding model (first time ~10s)...{RESET}")
        from sentence_transformers import SentenceTransformer
        _ST_MODEL = SentenceTransformer("all-MiniLM-L6-v2")
        print(f"{GREEN}  ✓  Local embedding model loaded{RESET}")
    return _ST_MODEL

# ── Text Generation (Gemini with fallback & Auto-Backoff Patch) ─────────────────
def get_client() -> genai.Client:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("GEMINI_API_KEY not found in .env")
    return genai.Client(api_key=api_key)

def gemini_generate(prompt: str, temperature: float = 0.2,
                    max_tokens: int = 32000) -> str:
    """Generate text with automatic model fallback and rate limit recovery."""
    client     = get_client()
    last_error = None

    for model in GENERATE_MODELS:
        for attempt in range(5):
            try:
                response = client.models.generate_content(
                    model=model,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        temperature=temperature,
                        max_output_tokens=max_tokens,
                    )
                )

                # Check finish reason cleanly
                candidate = response.candidates[0] if response.candidates else None
                if candidate:
                    finish_reason = str(candidate.finish_reason)
                    if "MAX_TOKENS" in finish_reason:
                        print(f"{YELLOW}  ⚠  WARNING: Gemini hit token limit — response may be cut off!{RESET}")
                    elif "SAFETY" in finish_reason or "RECITATION" in finish_reason:
                        print(f"{YELLOW}  ⚠  WARNING: Gemini blocked this response ({finish_reason}) — skipping model.{RESET}")
                        break  # try next model

                if not response.text:
                    raise ValueError("Empty response from model")
                if model != GENERATE_MODELS[0]:
                    print(f"{YELLOW}  ⚠  Used fallback model: {model}{RESET}")
                else:
                    print(f"{GREEN}  ✓  Gemini response ({model}){RESET}")
                
                # Baseline 2-second buffer delay to pace rapid pipeline iterations safely
                time.sleep(2)
                return response.text.strip()

            except Exception as e:
                err_str = str(e)
                
                # Catch rate limits or prepayment exhaustion blocks
                if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                    wait_time = 4 * (2 ** attempt)
                    print(f"{YELLOW}  ⚠  [429 Quota/Prepay Limit] on {model}. Relieving pipeline load... Waiting {wait_time}s to retry (Attempt {attempt+1}/5)...{RESET}")
                    time.sleep(wait_time)
                    last_error = e
                    continue 
                    
                elif "503" in err_str or "UNAVAILABLE" in err_str:
                    wait = (attempt + 1) * 5
                    print(f"{YELLOW}  ⚠  {model} busy — waiting {wait}s (attempt {attempt+1}/5)...{RESET}")
                    time.sleep(wait)
                    last_error = e
                    continue
                    
                elif "404" in err_str or "NOT_FOUND" in err_str:
                    print(f"{YELLOW}  ⚠  {model} not found (404) — trying next model...{RESET}")
                    last_error = e
                    break
                else:
                    print(f"{YELLOW}  ⚠  {model} failed ({err_str[:80]}) — trying next...{RESET}")
                    last_error = e
                    break
                    
        print(f"{YELLOW}  ⚠  {model} exhausted — shifting model layers...{RESET}")

    raise Exception(f"All Gemini models unavailable. Last error: {last_error}")

# ── Embeddings (local sentence-transformers PRIMARY) ──────────────────────────
def gemini_embed(content, task_type: str = "RETRIEVAL_DOCUMENT") -> list:
    is_list = isinstance(content, list)
    texts   = content if is_list else [content]

    try:
        model      = get_st_model()
        embeddings = model.encode(texts, show_progress_bar=False).tolist()
        print(f"{GREEN}  ✓  Local embeddings ({len(embeddings)} chunks){RESET}")
        return embeddings if is_list else embeddings[0]
    except Exception as e:
        print(f"{YELLOW}  ⚠  Local embed failed ({e}) — trying OpenAI...{RESET}")

    openai_key = os.getenv("OPENAI_API_KEY")
    if openai_key:
        try:
            from openai import OpenAI
            oa     = OpenAI(api_key=openai_key)
            result = oa.embeddings.create(model="text-embedding-3-small", input=texts)
            embs   = [item.embedding for item in result.data]
            print(f"{YELLOW}  ⚠  Used OpenAI embeddings{RESET}")
            return embs if is_list else embs[0]
        except Exception as e:
            print(f"{YELLOW}  ⚠  OpenAI embed failed ({str(e)[:60]}) — trying Gemini...{RESET}")

    gemini_key = os.getenv("GEMINI_API_KEY")
    if gemini_key:
        for embed_model in ["models/text-embedding-004", "models/embedding-001"]:
            try:
                client = genai.Client(api_key=gemini_key)
                result = client.models.embed_content(
                    model=embed_model, contents=content,
                    config=types.EmbedContentConfig(task_type=task_type)
                )
                if is_list:
                    return [e.values for e in result.embeddings]
                return result.embeddings[0].values
            except Exception as e:
                print(f"{YELLOW}  ⚠  Gemini embed {embed_model} failed — trying next...{RESET}")

    raise Exception("All embedding models unavailable. Check your .env file.")

# ── Robust JSON parser with Force-Repair Logic ────────────────────────────────
def parse_json_robust(raw: str) -> dict:
    import json, re
    if not raw or not raw.strip():
        print(f"{RED}  ✗  ERROR: Gemini returned an empty response!{RESET}")
        raise ValueError("Gemini returned an empty response.")

    raw = raw.strip()
    print("\n========== GEMINI RESPONSE ==========")
    print(raw)
    print("=====================================\n")

    if "```" in raw:
        parts = raw.split("```")
        for part in parts:
            if part.startswith("json"):
                raw = part[4:].strip()
                break
            elif "{" in part:
                raw = part.strip()
                break

    start = raw.find("{")
    if start == -1:
        raise ValueError("No JSON object found in response")
    raw = raw[start:]

    # Direct parse try
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # Regex try for closed structures
    match = re.search(r"\{[\s\S]*\}", raw)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass

    # Dynamic closing verification
    depth   = 0
    last_ok = -1
    in_str  = False
    escape  = False
    for i, ch in enumerate(raw):
        if escape:
            escape = False
            continue
        if ch == "\\" and in_str:
            escape = True
            continue
        if ch == '"' and not escape:
            in_str = not in_str
            continue
        if not in_str:
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    last_ok = i
                    break

    if last_ok > 0:
        try:
            return json.loads(raw[:last_ok+1])
        except:
            pass

    # EMERGENCY AGGRESSIVE REPAIR (Fixes cut-off JSON fields)
    print(f"{YELLOW}  ⚠  Targeting truncated json properties. Initiating structural patch...{RESET}")
    try:
        fixed = raw.strip()
        
        # If it ends right on a property colon or comma, slice it away safely
        if fixed.endswith(":") or fixed.endswith(","):
            fixed = re.sub(r',?\s*["\w_]*\s*:\s*$', '', fixed)
            fixed = fixed.rstrip(",")
            
        # Strip incomplete trailing text markers
        fixed = re.sub(r',\s*["\w_]*$', '', fixed)
        fixed = fixed.strip()
        
        # Balance out any quotes if caught in a cut-off text string value
        if fixed.count('"') % 2 != 0:
            fixed += '"'
            
        # Formulate formal closing dictionary markers
        if not fixed.endswith("}"):
            fixed += "}"
            
        return json.loads(fixed)
    except Exception as parse_err:
        print(f"{RED}  ✗  Emergency repair failed: {parse_err}{RESET}")

    raise json.JSONDecodeError("Could not parse JSON from response", raw, 0)

def retrieve_book_context_safe(query: str, top_k: int = 4) -> str:
    try:
        from pinecone import Pinecone
        pinecone_key = os.getenv("PINECONE_API_KEY")
        index_name   = os.getenv("PINECONE_INDEX_NAME", "dealdesk-knowledge")
        if not pinecone_key:
            return "Book context unavailable — no Pinecone key."
        pc    = Pinecone(api_key=pinecone_key)
        index = pc.Index(index_name)
        qv    = gemini_embed(query, task_type="RETRIEVAL_QUERY")
        hits  = index.query(vector=qv, top_k=top_k, include_metadata=True)["matches"]
        if not hits:
            return "No relevant passages found in knowledge base."
        return "\n\n---\n\n".join(
            f"[{m['metadata']['author']} | score:{round(m['score'],2)}]\n{m['metadata']['text'][:600]}"
            for m in hits
        )
    except Exception as e:
        return f"Book context unavailable: {e}"