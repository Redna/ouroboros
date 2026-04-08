# tests/test_agent_state_properties.py
from hypothesis import given, strategies as st
from pathlib import Path
import json
import sys
import os

# Ensure the project root is in the sys.path to import agent_state
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
import agent_state

import tempfile

@given(st.dictionaries(st.text(), st.text()))
def test_state_serialization_invariants(state_data: dict):
    """
    Property: An agent state saved to disk must be identical when loaded, 
    regardless of the arbitrary string contents.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        test_file = Path(tmp_dir) / "state.json"
        
        # Write the arbitrary state
        test_file.write_text(json.dumps(state_data), encoding="utf-8")
        
        # Load via the agent's safe loader
        # We pass an empty dict as default_structure to match the expected type
        loaded_data = agent_state.safe_load_json(test_file, {})
        
        # Invariant: The loaded data must match the written data exactly
        assert loaded_data == state_data
