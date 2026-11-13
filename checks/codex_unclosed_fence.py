"""Check that an unclosed fence does not discard the final context section."""
from json import dumps
from os import getcwd
from sys import exit, path

path[:0] = [getcwd()]
from coldrecall import packet_records

text = "## Freeze\nThe release freeze starts Friday.\n\n```python\nprint(1)\n"
records = packet_records("NOTES", text)
ok = len(records) == 1 and records[0]["heading"] == "Freeze" and "The release freeze starts Friday." in records[0]["lines"]
print(dumps({"status": "PASS"} if ok else {"status": "FAIL", "failure": "unclosed-fence-drops-final-section"}))
exit(0 if ok else 1)
