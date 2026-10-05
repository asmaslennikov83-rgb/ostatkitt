from pathlib import Path
from tempfile import TemporaryDirectory

from app.exclusions import create_exclusions_template, read_exclusions


def test_runtime_template_generation():
    with TemporaryDirectory() as td:
        path = Path(td) / "nested" / "exclusions.xlsx"
        assert not path.exists()
        create_exclusions_template(path, ("seller-1", "seller-2"))
        assert path.exists() and path.stat().st_size > 0
        data = read_exclusions(path, ("cab1", "cab2"), ("seller-1", "seller-2"))
        assert data == {"cab1": set(), "cab2": set()}


if __name__ == "__main__":
    test_runtime_template_generation()
    print("OK exclusions download template")
