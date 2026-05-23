"""Compara versiones de dependencias críticas entre PCs."""

import sys

def check_versions():
    print(f"Python: {sys.version}")
    print("=" * 80)
    
    deps = [
        "langchain",
        "langchain_core",
        "langchain_community",
        "langchain_chroma",
        "chromadb",
        "sentence_transformers",
        "transformers",
        "torch",
    ]
    
    for dep in deps:
        try:
            mod = __import__(dep)
            version = getattr(mod, "__version__", "sin __version__")
            print(f"{dep:25s} {version}")
        except ImportError:
            print(f"{dep:25s} NO INSTALADO")

if __name__ == "__main__":
    check_versions()
