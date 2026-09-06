"""Stemify audio separation worker.

Pipeline responsibility: retrieve source, validate audio, run model inference,
encode stems, package results, and report progress to the web application.
"""

__version__ = "0.1.0"
