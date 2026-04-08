import pytest
from hypothesis import given, strategies as st
from pathlib import Path
import tempfile
import constants
from capabilities.base_tools import _resolve_safe_path, patch_file, _normalize_text

def test_resolve_safe_path_within_bounds():
    # Test a path that is clearly within ROOT_DIR
    relative_path = "test_file.txt"
    resolved = _resolve_safe_path(relative_path)
    assert resolved == constants.ROOT_DIR / relative_path
    assert str(resolved).startswith(str(constants.ROOT_DIR))

@given(st.text(alphabet="abcdefghijklmnopqrstuvwxyz", max_size=50))
def test_resolve_safe_path_permission_error(random_string):
    # Test that paths outside the allowed directories raise PermissionError
    outside_path = Path(f"/tmp/{random_string}_safe_suffix")
    with pytest.raises((PermissionError, FileNotFoundError)):
        _resolve_safe_path(outside_path)

@given(
    prefix=st.text(alphabet="abcdefghijklmnopqrstuvwxyz\n ", min_size=10, max_size=100),
    suffix=st.text(alphabet="abcdefghijklmnopqrstuvwxyz\n ", min_size=10, max_size=100),
    original_target=st.text(alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ", min_size=5, max_size=20),
    replacement=st.text(alphabet="1234567890", min_size=5, max_size=20)
)
def test_patch_file_invariants(prefix, suffix, original_target, replacement):
    """
    Property: patching a file must ONLY alter the targeted block.
    The prefix and suffix must remain mathematically identical (after normalization).
    """
    with tempfile.TemporaryDirectory(dir=constants.ROOT_DIR) as tmp_dir:
        test_file = Path(tmp_dir) / "target_file.txt"
        
        # Construct the file state
        original_content = f"{prefix}\n{original_target}\n{suffix}"
        test_file.write_text(original_content, encoding="utf-8")
        
        # Execute the tool
        args = {
            "path": str(test_file),
            "search_text": original_target,
            "replace_text": replacement
        }
        result = patch_file(args)
        
        assert "Success" in result
        
        # Verify the invariant
        new_content = test_file.read_text(encoding="utf-8")
        expected_content = _normalize_text(f"{prefix}\n{replacement}\n{suffix}")
        
        # Invariant: the final file must match the normalized expected content
        assert new_content == expected_content

if __name__ == "__main__":
    pytest.main([__file__])
