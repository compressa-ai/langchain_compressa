from langchain_compressa import __all__

EXPECTED_ALL = [
    "CompressaLLM",
    "CompressaEmbeddings",
]


def test_all_imports() -> None:
    assert sorted(EXPECTED_ALL) == sorted(__all__)
