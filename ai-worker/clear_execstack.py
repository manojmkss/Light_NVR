"""Clear the "needs an executable stack" flag on the ONNX Runtime libraries.

Some onnxruntime wheels (onnxruntime-openvino in particular) are linked with
an executable stack (PT_GNU_STACK with PF_X) they don't actually need. Newer
kernels and glibc refuse to load such a library:

    ImportError: ...onnxruntime_pybind11_state...so: cannot enable executable
    stack as shared object requires: Invalid argument

and the AI service then can't run at all. Clearing the flag is the standard
fix (what `execstack -c` / `patchelf --clear-execstack` do); run at image build.

    python3 clear_execstack.py <site-packages dir>
"""

import os
import struct
import sys

PT_GNU_STACK = 0x6474E551
PF_X = 0x1


def clear(path: str) -> bool:
    with open(path, "r+b") as f:
        ident = f.read(16)
        if ident[:4] != b"\x7fELF" or ident[4] != 2:  # 64-bit ELF only
            return False
        end = "<" if ident[5] == 1 else ">"
        f.seek(0x20)
        (phoff,) = struct.unpack(end + "Q", f.read(8))
        f.seek(0x36)
        phentsize, phnum = struct.unpack(end + "HH", f.read(4))
        for i in range(phnum):
            at = phoff + i * phentsize
            f.seek(at)
            p_type, p_flags = struct.unpack(end + "II", f.read(8))
            if p_type == PT_GNU_STACK and p_flags & PF_X:
                f.seek(at + 4)
                f.write(struct.pack(end + "I", p_flags & ~PF_X))
                return True
    return False


def main(root: str) -> None:
    fixed = []
    for base, _dirs, files in os.walk(root):
        if "onnxruntime" not in base and "openvino" not in base:
            continue
        for name in files:
            if ".so" in name:
                p = os.path.join(base, name)
                try:
                    if clear(p):
                        fixed.append(p)
                except OSError:
                    pass
    for p in fixed:
        print("cleared executable-stack flag:", p)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "/usr/local/lib")
