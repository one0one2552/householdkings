import re

with open("main.py", encoding="utf-8") as f:
    src = f.read()

# Only replace: db_var.query(Model).get(id)  ->  db_var.get(Model, id)
# Safe simple case – no chained methods between .query() and .get()
src = re.sub(
    r'(\w+)\.query\((\w+)\)\.get\(([^)]+)\)',
    r'\1.get(\2, \3)',
    src
)

# Only replace single-line: db_var.query(Model).options(X, Y).get(id)
# where options() content has balanced parens and .get() follows directly
src = re.sub(
    r'(\w+)\.query\((\w+)\)\.options\(([^()]+(?:\([^()]*\)[^()]*)*)\)\.get\(([^)]+)\)',
    r'\1.get(\2, \4, options=[\3])',
    src
)

with open("main.py", "w", encoding="utf-8") as f:
    f.write(src)

print("Done")
