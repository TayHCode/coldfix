import argparse

parser = argparse.ArgumentParser()
parser.add_argument("--name", required=True)
print("hello", parser.parse_args().name)
