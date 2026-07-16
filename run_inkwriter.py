#!/usr/bin/env python3
"""Launch Inkwriter from the project root."""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from inkwriter.main import main
main()
