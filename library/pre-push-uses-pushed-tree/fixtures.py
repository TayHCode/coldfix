FIXTURES = {
    "bad": {'pre-push': 'git diff --name-only "$1" "$2" | while read -r file; do\n  [ -f "$file" ] && check "$file"\ndone\n'},
    "good": {'pre-push': 'git diff --name-only "$1" "$2" | while read -r file; do\n  git cat-file -e "$2:$file" && check "$2:$file"\ndone\n'},
}
