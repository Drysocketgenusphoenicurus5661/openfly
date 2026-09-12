"""Connectome data pipeline: download, verify, normalize and compile MaleCNS v1.0.

Public entry points:

    from openfly.connectome import sources, download, verify, normalize, compile
    compile.compile_graph()      # writes data/graph.npz, graph-manifest.json, graph.lock.json
    verify.verify()              # checks source hashes and compiled array hashes
"""
