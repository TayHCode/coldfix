def changes_line(hunk, line):
    old_start, old_count, _new_start, _new_count = hunk
    return old_start <= line <= old_start + max(old_count - 1, 0)
