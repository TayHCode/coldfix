TEST_SHA256 = "543600a1e1ab2cec7ac9ae2806a17528df0ae1956482bcc0dae45a0e433e46bb"
EXPECTED_FAILURE = 'regression:543600a1e1ab2cec'
PAYLOAD = (
    '78dae5596d6fdb3610fe2b07ed8392f8bd7ea96d38468b02c30674fdd0f65b5c',
    'a894748a84c89446d28dbd20ff7d47ca9115478ee52c5956cf066c89c73b1e9f',
    '7b48dd5137961fb14b9e481579d618ac5fdffffe710c0aa57222ee2357e83b02',
    '0314c83d747c0c221ea928e1d2aa83c5d91c25695d5846c15fa471e43185bb35',
    '2a1aaed0cd91612294a398ebc4c82f5598ebcd195fed319d32c12e054b43c7c5',
    '2011fb1c2e7760adeac68977f5e7225158adbf97f86ba56afd374d8e247c95c3',
    '3c9148e9c411c78a2e864c864e88cc475151a162bf1cc76add252a5c2a674181',
    '15dafd8a5a66d61bc11e2d89da2bcd1b7755d94115a9181d8238b9e60659eb1b',
    '29a44c79a15e157e1404d0685c460a584b9b91e6d7912baed8b265a2b96edad8',
    '273357b299aec03d5865c635f997301a76fda0f706fb9dfedbee6014e0a8ddc7',
    '6ec7f73c3f18f55c16f4db5e1747c366b3ef8d86c39edb1e8d0643175da4f6e1',
    '60e80d06dd2ef6bbdea8ed0f47accb02e8b4db835e6fc61b8dc613e632e3b55a',
    'ed49137af70e1a834ebdf3166afaafd7016af16226257c259dcf772a1fb5c689',
    '6efac0249e8e671c8a1f4da03a04f6249ddee8ebdb492b9dda75c0658a9e0e3a',
    '534ac8f31b3bf741da63b8b9bd2dd839a56b02405f120760272db6689b2da013',
    '8971a0ddaaedd5dfc9fbdc46eed2cc32dfcc29fd790fb945d858842089e3e41a',
    '7d7057c020b30bb95d8824b5667e12243f90172c5e87c881084c7d52811efa6b',
    '2b3c51218ac220cdf5dc32371e8cc10245dd3d9a1e7a0b15fdc0524f65e64bbe',
    'feea05a38cfbe02728818606b5101cc803202aa8e25423ae927c36cd12acf206',
    '8d6693888442fdc1c4954f6bf8336a7fe5c9167bec0bf66d0ca152e9b8d5c225',
    '9ba73136bd64de62b319bf70cb652ec9cecfcfed7a014c636c1276a63b95262d',
    '92dac569673ae9946c65a4dd926d53784b6cd4efb3da66f47b6297cd8796c4a7',
    '84e3691d6c775727f7aed3edd650c57b5a2cb5c3c8beb55ddf5f30c500165856',
    '88bbd187ec2107e621b7a6dc364f0dc5620c14ad0aa12954b0c8356753e2441d',
    'a4615122a2bf12daa862108b1841f34b49620ef1d887bbb563f8c8ae689d3099',
    '719236a3e6639e3f4abdda41d4a3bfb3b3b352c668b1a60c759984025a863bf0',
    '18770e274ac90e7940d01f4d521e32e079612ba7f6b6a1a35b6f0f729617c579',
    'd7ce38b3be6a3f66d67f1571dbf8673fef26973fd75e05f32fc91c4d2654ba5b',
    'e4d29ffc2953ad48fb4701c8a1d2803f2518471d8043eaab175b07f742f4bf0c',
    'c3e3e7202f063cc90fc0fb68f17d70ce74683e7b675e175bdf3766be53a2cb68',
    'e6e047548da978b5ce72d7f558c15ad196aea936f77593b1b2c218fa70094c29',
    'fe6fa4ad47c3906dd736613a7fb3873d7b0e3d5f2e3f60cf877c6598dd62b5f0',
    'f4eca9701ef92af9d32ff09b19bce4b141457b262bafdf8fa4460f5f07f7f2fa',
    '36af688f11e9cddb8157417c0a66ec12d4378e996385746aaeef4e195a05f1b1',
    '8666f322e65542a395b50b65b9abc0e944cba6fac7c4c3dc5174c44f160fb817',
    '8fddafe6761f89e70d1ff61e3953aab68829d142733cbd985342a4924b34e786',
    '54b298b4cacadeeb98d73a87bec5b06eff0642d4307c',
)

import hashlib
import json
import os
import re
import shutil
import sys
from subprocess import DEVNULL, run
import tempfile
import zlib

def apply(root, patch_file, *options):
    return run(["git", "apply", "--whitespace=nowarn", *options, patch_file], cwd=root,
               stdout=DEVNULL, stderr=DEVNULL).returncode


def main():
    data = json.loads(zlib.decompress(bytes.fromhex("".join(PAYLOAD))))
    patch = data["patch"].encode("utf-8")
    if hashlib.sha256(patch).hexdigest() != TEST_SHA256:
        return 2
    names = data["names"]
    with tempfile.TemporaryDirectory() as temporary:
        root = os.path.join(temporary, "tree")
        shutil.copytree(".", root, symlinks=True, ignore=shutil.ignore_patterns(".git"))
        patch_file = os.path.join(temporary, "tests.patch")
        with open(patch_file, "wb") as stream:
            stream.write(patch)
        if apply(root, patch_file, "-R", "--check") != 0 and apply(root, patch_file) != 0:
            return 2
        directory, name = os.path.split(data["path"])
        command = [sys.executable, "-m", "unittest", "discover", "-s", directory or ".", "-p", name]
        for test in names:
            command += ["-k", test]
        result = run(command, cwd=root, capture_output=True, text=True, errors="replace",
                     env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
    output = result.stdout + result.stderr
    ran = re.findall(r"^Ran ([0-9]+) tests? in ", output, re.M)
    if not ran or int(ran[0]) < max(1, len(names)):
        return 2
    if result.returncode == 0 and re.findall(r"^OK$", output, re.M):
        print('{"status": "PASS"}')
        return 0
    if result.returncode == 1 and data["diagnostic"] in output and re.findall(r"^FAILED \(", output, re.M):
        print('{"status": "FAIL", "failure": "' + EXPECTED_FAILURE + '"}')
        return 1
    return 2


sys.exit(main())
