import os
import sys
import subprocess
from pathlib import Path
from openai import OpenAI

# We import from the local constants if possible, otherwise use defaults
try:
    sys.path.append(str(Path(__file__).parent.parent))
    import constants
    API_BASE = constants.API_BASE
    MODEL = constants.MODEL
except ImportError:
    API_BASE = "http://gate:4000/v1"
    MODEL = "mistralai_Mistral-Small-3.2-24B-Instruct-2506-Q4_K_M.gguf"

def get_staged_diff() -> str:
    result = subprocess.run(
        ["git", "diff", "--staged"], 
        capture_output=True, 
        text=True
    )
    return result.stdout.strip()

def run_audit() -> None:
    # Bypass for Creator/Maintenance
    if os.environ.get("SKIP_CONSTITUTIONAL_AUDIT") == "1":
        print("[Auditor] Bypass requested by Creator. Skipping semantic check.")
        sys.exit(0)

    diff = get_staged_diff()
    if not diff:
        sys.exit(0)  # Nothing to audit

    root_dir = Path(__file__).parent.parent
    constitution_path = root_dir / "CONSTITUTION.md"
    
    if not constitution_path.exists():
        print("[Auditor] Warning: CONSTITUTION.md not found. Skipping audit.")
        sys.exit(0)

    constitution = constitution_path.read_text(encoding="utf-8")
    
    client = OpenAI(base_url=API_BASE, api_key="sk-not-required")

    prompt = f"""You are the Constitutional Auditor. Your ONLY job is to prevent the agent from violating its core principles.
Read the CONSTITUTION.
Read the GIT DIFF.

If the diff violates any P0-P9 principle (e.g., adds hardcoded personality, adds complexity over 1000 lines, removes core memory constraints), you must reject it.

Respond in exactly this format:
RESULT: [PASS or FAIL]
REASON: [One sentence explaining why]

CONSTITUTION:
{constitution}

GIT DIFF:
{diff}
"""

    try:
        response = client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "system", "content": prompt}],
            temperature=0.0,
            max_tokens=100
        )
        
        output = response.choices[0].message.content.strip()
        print(f"\n[Constitutional Auditor]\n{output}\n")
        
        if "RESULT: FAIL" in output.upper():
            sys.exit(1)
        sys.exit(0)
            
    except Exception as e:
        print(f"[Auditor] Error connecting to LLM backend: {e}. Failing open to prevent lockout.")
        sys.exit(0)

if __name__ == "__main__":
    run_audit()
