#!/usr/bin/env bash
# Run from repository root; generated CSS is committed. No Node/CDN needed at runtime.
set -euo pipefail
npx --yes --package=tailwindcss@3.4.17 tailwindcss --config webapp/static/tailwind.config.cjs --input webapp/static/desk.css --output webapp/static/uoa.css --minify
