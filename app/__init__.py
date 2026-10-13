"""Deerstalker local file-investigation application package.

The pipeline runs in this order:

* discovery and archive expansion;
* fast triage;
* detailed analysis of qualifying files;
* output copying and report generation;
* the local FastAPI surface used by the frontend.
"""
