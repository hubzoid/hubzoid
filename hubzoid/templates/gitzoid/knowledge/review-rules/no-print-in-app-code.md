---
title: No print in app code
paths: ["app/**/*.py", "app/*.py"]
severity: medium
pattern: '^\s*print\('
---

Use the module's `log` instead of `print`. Printed lines never reach the log
store, so nobody sees them in production.
