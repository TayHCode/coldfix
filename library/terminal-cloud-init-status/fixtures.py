FIXTURES = {
    "bad": {'boot.sh': 'cloud-init status --wait || exit 1\n'},
    "good": {'boot.sh': 'cloud-init status --wait || [ $? -eq 2 ] || exit 1\n'},
}
