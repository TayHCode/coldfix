TEST_SHA256 = "e92d40599fedda1b445b3a9d487f942b53672ed354048098f6bf2181a04643ca"
EXPECTED_FAILURE = 'regression:e92d40599fedda1b'
PAYLOAD = (
    '78da9d56616fdb3610fd2b07ed8b8c488ad3055d17c04082361e82b549d0789f',
    'e280a025cae62a939e48253186fdf7dd91b24ccb361ac4306c51ba7bf7eeeef1',
    'a87fa342f2b9d2c6ca3cba80687c75f3f502ac309619be147a2554292bc1dc9d',
    'a57e160593ca6a56f21f82d113b336562cddf3288148a19341a0c7e89d184f08',
    'b2e2365f109b429625a4e95c5ae0a7ce3dd75551ca57b6aab52ed987b3dfcfcb',
    '5ff999f8247ecb566b98bdc168aa9478010a0b4b5d08381b0e3f9e9f4f559aa6',
    '705a88e753d554d5549d9c9cbc11eef212d261328493b3e4e307b8bc445fb95c',
    'e91a391bbb5d68b3bd6e94b4048d77f0fbfdfafe8e7dbfbb9bc008ad32cc7e91',
    '15b2a652c69b359f19fa8f992b186383013a8eaffebc66e30736b97e087dffd6',
    '52c51d6802d368b5a65a97661ad18a22b797875a40494504ffd7edcd84a00fe2',
    'e373c0cffbc26cf2dfc643b881af067e0b51025bf1da8898020e2eda602fd22e',
    '80e4e46e272054ae0ba9e6a369d4d832fd84b4b1e6506eece9530bdbd48a5a91',
    '79c432ab052fe241e23440451eb920bbd14ba90a9657dc98d8d64224e0ae1999',
    '77744a5d83220949056494cd74b10e63cb12a491ca58ae72119369e2887c26ac',
    '2fa244b6aa70101901c368148409818244c8ba7dd2deb9d54aec925f0abbd085',
    '4331718b886e1df1d6f151f9b02e0f4a626bea52e9f3f7e4c78dcaadd48af83f',
    '75719d2b3c20dc1df6678c959d606bbfea9c932d5dc79ba667b4facccd960f91',
    '7edfb8888da8ca4158aa5f60b2100e0d960dfe54f2d935688cdef7d8e76fba68',
    '3cb904709389dc566be0a515f59602054976308d06bbe0167010d58d32c0e79c',
    'ea02338d8ab41890c8b96ed2020556f909e3a9660196cfc230120ceea956e6e1',
    '4e1e04d6a44cac015176f9924720cd108cf6d97e927e276fe0a85a193a8adade',
    '985b6d493a713fc66120149e45a534aaf09001aad31931dbd15d1ff6180f1c25',
    'fb67058d0d07137a61dee215e3b807995b1df63d12ea0fec0bf639769e090c8f',
    '985dffd3f02a76411e7dc814ce9e3613ad13c85e19bcf29c6003fd4995570dce',
    '08ee54919267ea5501b31af7d52214872f20a5285e6ddc1b00aab7eeb66dbfd2',
    '6ef3f68c7fba95fd28eae6d0c1ba069861ed1a230ca3ec30fa72859b0a13e06a',
    'dde77f70145e595bcb596345300b39def31c2682ce4b5eafbfb89daaebf50e89',
    'fe1026c0175efd887d1d0747f806bd9ed48d88f7f82787d287051e2d4ab76d34',
    '6df78ea840d7722e154e815ce36b099d0f787333005b75b8867991cc686638e5',
    '846ad89e923bb362e758ee0d8b7c21ea438362072a719535f6dedbbf6d4474c8',
    '3deffe5c38808328c777f9ded0e8020db64732aa97390bc6bc30185be200666c',
    '1a6d667f77bad083185d23ff22e9de237ffe1a17fdf73fe69baf1e',
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
