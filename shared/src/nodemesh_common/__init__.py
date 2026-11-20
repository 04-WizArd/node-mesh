"""Code shared by the NodeMesh coordinator and storage nodes.

Only protocol-level concerns live here: the internal request-signing scheme and
the structured log formatter. Business logic belongs to each Django project.
"""
