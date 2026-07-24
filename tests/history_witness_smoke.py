#!/usr/bin/env python3
"""Offline contract tests for the production witness verifier boundary."""

import hashlib
import importlib
import importlib.util
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
REQUEST_DOMAIN = b"history-calibration-witness-request-v1\0"
AUTO_INTERPRETER_SHA256 = object()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def canonical(value):
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        + "\n"
    ).encode("utf-8")


FAKE_VERIFIER = r'''
import hashlib
import json
import os
import pathlib
import subprocess
import sys
import time

DOMAIN = b"history-calibration-witness-request-v1\0"


def canonical(value):
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        + "\n"
    ).encode("utf-8")


mode = EMBEDDED_MODE
log_path = pathlib.Path(EMBEDDED_LOG_PATH)
raw = sys.stdin.buffer.read()
log_path.write_bytes(raw)
pathlib.Path(str(log_path) + ".argv").write_bytes(
    canonical(sys.argv[1:])
)
pathlib.Path(str(log_path) + ".process").write_bytes(
    canonical(
        {
            "cwd": os.getcwd(),
            "environment": dict(os.environ),
            "interpreter": sys.executable,
            "script": sys.argv[0],
        }
    )
)

if mode == "timeout":
    time.sleep(2)
if mode == "oversized-stdout":
    sys.stdout.buffer.write(b"x" * 8192)
    raise SystemExit(0)
if mode == "oversized-stderr":
    sys.stderr.buffer.write(b"x" * 32768)
    raise SystemExit(0)
if mode == "nonzero":
    raise SystemExit(7)
if mode == "invalid-utf8":
    sys.stdout.buffer.write(b"\xff\n")
    raise SystemExit(0)

response = {
    "schema_version": 1,
    "protocol": "history-calibration-witness-v1",
    "request_sha256": hashlib.sha256(DOMAIN + raw).hexdigest(),
    "verified": mode != "false",
}
if mode == "wrong-request":
    response["request_sha256"] = "0" * 64
if mode == "extra-field":
    response["extra"] = True
if mode == "bool-version":
    response["schema_version"] = True
if mode == "noncanonical":
    sys.stdout.write(json.dumps(response, indent=2) + "\n")
    raise SystemExit(0)
if mode == "trailing":
    sys.stdout.buffer.write(canonical(response) + b"trailing")
    raise SystemExit(0)
if mode == "mutate":
    with pathlib.Path(EMBEDDED_EXECUTABLE_PATH).open(
        "ab"
    ) as executable:
        executable.write(b"\n# drift\n")
if mode == "mutate-interpreter":
    with pathlib.Path(EMBEDDED_INTERPRETER_PATH).open(
        "ab"
    ) as interpreter:
        interpreter.write(b"\n")
if mode == "spawn-descendant":
    marker = pathlib.Path(str(log_path) + ".descendant")
    child_code = (
        "import pathlib,time\n"
        "time.sleep(0.35)\n"
        f"pathlib.Path({str(marker)!r}).write_text("
        "'survived', encoding='utf-8')\n"
    )
    subprocess.Popen(
        [sys.executable, "-c", child_code],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    sys.stdout.buffer.write(b"ok")
    raise SystemExit(0)

sys.stdout.buffer.write(canonical(response))
'''

NATIVE_VERIFIER_SOURCE = r'''
#define _POSIX_C_SOURCE 200809L
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <time.h>
#include <unistd.h>
#ifdef __APPLE__
#include <mach-o/dyld.h>
#endif

#ifndef PATH_MAX
#define PATH_MAX 4096
#endif

extern char **environ;

typedef struct {
    uint8_t data[64];
    uint32_t datalen;
    uint64_t bitlen;
    uint32_t state[8];
} sha256_context;

static const uint32_t sha256_constants[64] = {
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5,
    0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3,
    0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc,
    0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7,
    0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13,
    0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3,
    0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5,
    0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208,
    0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2
};

static uint32_t rotate_right(uint32_t value, uint32_t count) {
    return (value >> count) | (value << (32 - count));
}

static void sha256_transform(
    sha256_context *context, const uint8_t data[64]
) {
    uint32_t words[64];
    for (size_t index = 0; index < 16; ++index) {
        size_t offset = index * 4;
        words[index] =
            ((uint32_t)data[offset] << 24)
            | ((uint32_t)data[offset + 1] << 16)
            | ((uint32_t)data[offset + 2] << 8)
            | (uint32_t)data[offset + 3];
    }
    for (size_t index = 16; index < 64; ++index) {
        uint32_t s0 =
            rotate_right(words[index - 15], 7)
            ^ rotate_right(words[index - 15], 18)
            ^ (words[index - 15] >> 3);
        uint32_t s1 =
            rotate_right(words[index - 2], 17)
            ^ rotate_right(words[index - 2], 19)
            ^ (words[index - 2] >> 10);
        words[index] =
            words[index - 16] + s0 + words[index - 7] + s1;
    }
    uint32_t a = context->state[0];
    uint32_t b = context->state[1];
    uint32_t c = context->state[2];
    uint32_t d = context->state[3];
    uint32_t e = context->state[4];
    uint32_t f = context->state[5];
    uint32_t g = context->state[6];
    uint32_t h = context->state[7];
    for (size_t index = 0; index < 64; ++index) {
        uint32_t s1 =
            rotate_right(e, 6)
            ^ rotate_right(e, 11)
            ^ rotate_right(e, 25);
        uint32_t choice = (e & f) ^ ((~e) & g);
        uint32_t first =
            h + s1 + choice + sha256_constants[index]
            + words[index];
        uint32_t s0 =
            rotate_right(a, 2)
            ^ rotate_right(a, 13)
            ^ rotate_right(a, 22);
        uint32_t majority = (a & b) ^ (a & c) ^ (b & c);
        uint32_t second = s0 + majority;
        h = g;
        g = f;
        f = e;
        e = d + first;
        d = c;
        c = b;
        b = a;
        a = first + second;
    }
    context->state[0] += a;
    context->state[1] += b;
    context->state[2] += c;
    context->state[3] += d;
    context->state[4] += e;
    context->state[5] += f;
    context->state[6] += g;
    context->state[7] += h;
}

static void sha256_initialize(sha256_context *context) {
    context->datalen = 0;
    context->bitlen = 0;
    context->state[0] = 0x6a09e667;
    context->state[1] = 0xbb67ae85;
    context->state[2] = 0x3c6ef372;
    context->state[3] = 0xa54ff53a;
    context->state[4] = 0x510e527f;
    context->state[5] = 0x9b05688c;
    context->state[6] = 0x1f83d9ab;
    context->state[7] = 0x5be0cd19;
}

static void sha256_update(
    sha256_context *context, const uint8_t *data, size_t length
) {
    for (size_t index = 0; index < length; ++index) {
        context->data[context->datalen++] = data[index];
        if (context->datalen == 64) {
            sha256_transform(context, context->data);
            context->bitlen += 512;
            context->datalen = 0;
        }
    }
}

static void sha256_finalize(
    sha256_context *context, uint8_t digest[32]
) {
    size_t index = context->datalen;
    context->data[index++] = 0x80;
    if (index > 56) {
        while (index < 64) {
            context->data[index++] = 0;
        }
        sha256_transform(context, context->data);
        index = 0;
    }
    while (index < 56) {
        context->data[index++] = 0;
    }
    context->bitlen += (uint64_t)context->datalen * 8;
    for (size_t offset = 0; offset < 8; ++offset) {
        context->data[63 - offset] =
            (uint8_t)(context->bitlen >> (offset * 8));
    }
    sha256_transform(context, context->data);
    for (size_t word = 0; word < 8; ++word) {
        digest[word * 4] =
            (uint8_t)(context->state[word] >> 24);
        digest[word * 4 + 1] =
            (uint8_t)(context->state[word] >> 16);
        digest[word * 4 + 2] =
            (uint8_t)(context->state[word] >> 8);
        digest[word * 4 + 3] =
            (uint8_t)context->state[word];
    }
}

static char *suffix_path(const char *path, const char *suffix) {
    size_t size = strlen(path) + strlen(suffix) + 1;
    char *result = malloc(size);
    if (result == NULL) {
        return NULL;
    }
    snprintf(result, size, "%s%s", path, suffix);
    return result;
}

static int write_suffix(
    const char *path,
    const char *suffix,
    const void *data,
    size_t length
) {
    char *destination = suffix_path(path, suffix);
    if (destination == NULL) {
        return -1;
    }
    FILE *stream = fopen(destination, "wb");
    free(destination);
    if (stream == NULL) {
        return -1;
    }
    int failed =
        fwrite(data, 1, length, stream) != length
        || fclose(stream) != 0;
    return failed ? -1 : 0;
}

static void current_executable(
    char result[PATH_MAX], const char *fallback
) {
#ifdef __APPLE__
    uint32_t size = PATH_MAX;
    if (_NSGetExecutablePath(result, &size) == 0) {
        return;
    }
#elif defined(__linux__)
    ssize_t length =
        readlink("/proc/self/exe", result, PATH_MAX - 1);
    if (length >= 0) {
        result[length] = '\0';
        return;
    }
#endif
    snprintf(result, PATH_MAX, "%s", fallback);
}

static int mode_is(const char *path, const char *expected) {
    const char *marker = strstr(path, "--mode=");
    return marker != NULL && strcmp(marker + 7, expected) == 0;
}

int main(int argc, char **argv) {
    if (argc != 1) {
        return 90;
    }
    size_t capacity = 4096;
    size_t length = 0;
    uint8_t *request = malloc(capacity);
    if (request == NULL) {
        return 91;
    }
    for (;;) {
        if (length == capacity) {
            if (capacity >= 2 * 1024 * 1024) {
                free(request);
                return 92;
            }
            capacity *= 2;
            uint8_t *larger = realloc(request, capacity);
            if (larger == NULL) {
                free(request);
                return 93;
            }
            request = larger;
        }
        ssize_t count = read(
            STDIN_FILENO, request + length, capacity - length
        );
        if (count == 0) {
            break;
        }
        if (count < 0) {
            if (errno == EINTR) {
                continue;
            }
            free(request);
            return 94;
        }
        length += (size_t)count;
    }
    if (write_suffix(argv[0], ".request", request, length) != 0
        || write_suffix(
            argv[0], ".request.argv", "[]\n", 3
        ) != 0) {
        free(request);
        return 95;
    }
    char working_directory[PATH_MAX];
    if (getcwd(working_directory, sizeof(working_directory)) == NULL
        || write_suffix(
            argv[0],
            ".request.cwd",
            working_directory,
            strlen(working_directory)
        ) != 0) {
        free(request);
        return 96;
    }
    char executable[PATH_MAX];
    current_executable(executable, argv[0]);
    if (write_suffix(
        argv[0],
        ".request.executable",
        executable,
        strlen(executable)
    ) != 0) {
        free(request);
        return 97;
    }
    char *environment_path = suffix_path(
        argv[0], ".request.environment"
    );
    if (environment_path == NULL) {
        free(request);
        return 98;
    }
    FILE *environment = fopen(environment_path, "wb");
    free(environment_path);
    if (environment == NULL) {
        free(request);
        return 99;
    }
    for (char **item = environ; *item != NULL; ++item) {
        fprintf(environment, "%s\n", *item);
    }
    if (fclose(environment) != 0) {
        free(request);
        return 100;
    }

    if (mode_is(argv[0], "timeout")) {
        struct timespec delay = {2, 0};
        nanosleep(&delay, NULL);
    }
    if (mode_is(argv[0], "oversized-stdout")) {
        uint8_t bytes[8192];
        memset(bytes, 'x', sizeof(bytes));
        write(STDOUT_FILENO, bytes, sizeof(bytes));
        free(request);
        return 0;
    }
    if (mode_is(argv[0], "oversized-stderr")) {
        uint8_t bytes[32768];
        memset(bytes, 'x', sizeof(bytes));
        write(STDERR_FILENO, bytes, sizeof(bytes));
        free(request);
        return 0;
    }
    if (mode_is(argv[0], "nonzero")) {
        free(request);
        return 7;
    }
    if (mode_is(argv[0], "invalid-utf8")) {
        const uint8_t invalid[] = {0xff, '\n'};
        write(STDOUT_FILENO, invalid, sizeof(invalid));
        free(request);
        return 0;
    }

    static const uint8_t domain[] =
        "history-calibration-witness-request-v1";
    sha256_context hash;
    uint8_t digest[32];
    char digest_text[65];
    sha256_initialize(&hash);
    sha256_update(&hash, domain, sizeof(domain));
    sha256_update(&hash, request, length);
    sha256_finalize(&hash, digest);
    free(request);
    for (size_t index = 0; index < 32; ++index) {
        snprintf(
            digest_text + index * 2,
            sizeof(digest_text) - index * 2,
            "%02x",
            digest[index]
        );
    }
    digest_text[64] = '\0';
    if (mode_is(argv[0], "wrong-request")) {
        memset(digest_text, '0', 64);
        digest_text[64] = '\0';
    }
    if (mode_is(argv[0], "mutate")) {
        int target = open(argv[0], O_WRONLY | O_APPEND);
        if (target >= 0) {
            write(target, "\n", 1);
            close(target);
        }
    }
    const char *version =
        mode_is(argv[0], "bool-version") ? "true" : "1";
    const char *verified =
        mode_is(argv[0], "false") ? "false" : "true";
    char response[512];
    if (mode_is(argv[0], "extra-field")) {
        snprintf(
            response,
            sizeof(response),
            "{\"extra\":true,\"protocol\":"
            "\"history-calibration-witness-v1\","
            "\"request_sha256\":\"%s\","
            "\"schema_version\":%s,\"verified\":%s}\n",
            digest_text,
            version,
            verified
        );
    } else if (mode_is(argv[0], "noncanonical")) {
        snprintf(
            response,
            sizeof(response),
            "{\n  \"protocol\": "
            "\"history-calibration-witness-v1\",\n"
            "  \"request_sha256\": \"%s\",\n"
            "  \"schema_version\": %s,\n"
            "  \"verified\": %s\n}\n",
            digest_text,
            version,
            verified
        );
    } else {
        snprintf(
            response,
            sizeof(response),
            "{\"protocol\":"
            "\"history-calibration-witness-v1\","
            "\"request_sha256\":\"%s\","
            "\"schema_version\":%s,\"verified\":%s}\n",
            digest_text,
            version,
            verified
        );
    }
    write(STDOUT_FILENO, response, strlen(response));
    if (mode_is(argv[0], "trailing")) {
        write(STDOUT_FILENO, "trailing", 8);
    }
    return 0;
}
'''


class WitnessContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.native_temporary = tempfile.TemporaryDirectory()
        native_root = pathlib.Path(cls.native_temporary.name)
        source = native_root / "native_witness.c"
        cls.native_verifier = native_root / "native_witness"
        source.write_text(
            NATIVE_VERIFIER_SOURCE, encoding="utf-8"
        )
        completed = subprocess.run(
            [
                "cc",
                "-std=c99",
                "-O2",
                str(source),
                "-o",
                str(cls.native_verifier),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                "native witness fixture compilation failed:\n"
                + completed.stderr.decode("utf-8", "replace")
            )

    @classmethod
    def tearDownClass(cls):
        cls.native_temporary.cleanup()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = pathlib.Path(self.temporary.name).resolve()
        self.counter = 0

    def write_script_verifier(
        self, path, *, mode, log, shebang=None
    ):
        path = pathlib.Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        interpreter = pathlib.Path(sys.executable).resolve()
        interpreter_path = shebang or interpreter
        path.write_text(
            (
                f"#!{interpreter_path}\n"
                f"EMBEDDED_MODE = {mode!r}\n"
                f"EMBEDDED_LOG_PATH = {str(log)!r}\n"
                f"EMBEDDED_EXECUTABLE_PATH = {str(path)!r}\n"
                f"EMBEDDED_INTERPRETER_PATH = "
                f"{str(interpreter_path)!r}\n"
                + FAKE_VERIFIER
            ),
            encoding="utf-8",
        )
        path.chmod(0o700)
        return path

    def write_verifier(self, path, *, mode, log=None):
        path = pathlib.Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.native_verifier, path)
        path.chmod(0o700)
        return path

    def module(self):
        specification = importlib.util.find_spec(
            "lib.history_witness"
        )
        self.assertIsNotNone(
            specification, "lib.history_witness is missing"
        )
        return importlib.import_module("lib.history_witness")

    def artifact(self, *, scope="production", root_id="prod-root"):
        return {
            "schema_version": 1,
            "scope": scope,
            "trust_root_id": root_id,
            "signature": "11" * 32,
        }

    def write_root(
        self,
        *,
        mode="ok",
        executable=None,
        executable_name=None,
        executable_sha256=None,
        argv0=None,
        shebang=None,
        script=False,
        interpreter_sha256=AUTO_INTERPRETER_SHA256,
        include_interpreter_sha256=True,
        extra_fields=None,
        canonical_root=True,
    ):
        self.counter += 1
        log = self.root / f"request-{self.counter}.json"
        if executable is None:
            executable = self.root / (
                executable_name
                or (
                    f"fake-witness-{self.counter}"
                    f"--mode={mode}"
                )
            )
            if script or shebang is not None:
                self.write_script_verifier(
                    executable,
                    mode=mode,
                    log=log,
                    shebang=shebang,
                )
            else:
                self.write_verifier(
                    executable, mode=mode, log=log
                )
        executable = pathlib.Path(executable)
        if not script and shebang is None:
            log = pathlib.Path(str(executable) + ".request")
        raw = executable.read_bytes()
        if interpreter_sha256 is AUTO_INTERPRETER_SHA256:
            interpreter_sha256 = None
            if raw.startswith(b"#!"):
                line_end = raw.find(b"\n")
                encoded_path = raw[2:line_end]
                try:
                    interpreter_raw = pathlib.Path(
                        os.fsdecode(encoded_path)
                    ).read_bytes()
                except OSError:
                    interpreter_raw = pathlib.Path(
                        sys.executable
                    ).resolve().read_bytes()
                interpreter_sha256 = hashlib.sha256(
                    interpreter_raw
                ).hexdigest()
        root = {
            "schema_version": 1,
            "scope": "production",
            "trust_root_id": "prod-root",
            "verifier_protocol":
                "history-calibration-witness-v1",
            "verifier_argv": [str(argv0 or executable)],
            "verifier_executable_sha256": (
                executable_sha256
                or hashlib.sha256(raw).hexdigest()
            ),
        }
        if include_interpreter_sha256:
            root["verifier_interpreter_sha256"] = (
                interpreter_sha256
            )
        if extra_fields:
            root.update(extra_fields)
        path = self.root / f"trust-root-{self.counter}.json"
        if canonical_root:
            path.write_bytes(canonical(root))
        else:
            path.write_text(
                json.dumps(root, indent=2) + "\n",
                encoding="utf-8",
            )
        return path, root, log

    def test_valid_verifier_receives_and_returns_canonical_binding(self):
        witness = self.module()
        root_path, root, log = self.write_root()
        artifact = self.artifact()
        result = witness.verify_production_artifact(
            root_path, "preheldout_receipt", artifact
        )
        request_raw = log.read_bytes()
        request = json.loads(request_raw.decode("utf-8"))
        self.assertEqual(
            pathlib.Path(str(log) + ".cwd").read_text(
                encoding="utf-8"
            ),
            "/",
        )
        staged_executable = pathlib.Path(
            pathlib.Path(str(log) + ".executable").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(staged_executable.name, "verifier")
        self.assertFalse(staged_executable.exists())
        environment = dict(
            line.split("=", 1)
            for line in pathlib.Path(
                str(log) + ".environment"
            ).read_text(encoding="utf-8").splitlines()
        )
        darwin_text_encoding = environment.pop(
            "__CF_USER_TEXT_ENCODING", None
        )
        self.assertEqual(
            environment,
            {
                "PATH": "/usr/bin:/bin",
                "LANG": "C",
                "LC_ALL": "C",
            },
        )
        if sys.platform == "darwin" and darwin_text_encoding is not None:
            self.assertEqual(
                darwin_text_encoding,
                f"0x{os.getuid():X}:0x0:0x0",
            )
        else:
            self.assertIsNone(darwin_text_encoding)
        self.assertEqual(request_raw, canonical(request))
        self.assertEqual(
            json.loads(
                pathlib.Path(str(log) + ".argv").read_text(
                    encoding="utf-8"
                )
            ),
            [],
        )
        self.assertEqual(
            set(request),
            {
                "schema_version",
                "protocol",
                "trust_root_id",
                "artifact_kind",
                "artifact_sha256",
                "artifact",
            },
        )
        self.assertEqual(request["artifact"], artifact)
        self.assertEqual(
            request["artifact_sha256"],
            hashlib.sha256(canonical(artifact)).hexdigest(),
        )
        request_sha = hashlib.sha256(
            REQUEST_DOMAIN + request_raw
        ).hexdigest()
        self.assertEqual(
            result,
            {
                "schema_version": 1,
                "trust_root_id": "prod-root",
                "trust_root_sha256": hashlib.sha256(
                    canonical(root)
                ).hexdigest(),
                "verifier_executable_sha256":
                    root["verifier_executable_sha256"],
                "verifier_interpreter_sha256":
                    root["verifier_interpreter_sha256"],
                "artifact_kind": "preheldout_receipt",
                "artifact_sha256":
                    request["artifact_sha256"],
                "request_sha256": request_sha,
            },
        )

    def test_canonical_bytes_rejects_lone_surrogate(self):
        witness = self.module()
        with self.assertRaises(witness.WitnessError):
            witness.canonical_bytes({"value": "\ud800"})

    def test_trust_root_is_closed_canonical_and_path_backed(self):
        witness = self.module()
        cases = (
            self.write_root(extra_fields={"unexpected": True}),
            self.write_root(
                extra_fields={"schema_version": True}
            ),
            self.write_root(canonical_root=False),
            self.write_root(argv0="relative-verifier"),
            self.write_root(executable_sha256="0" * 64),
        )
        for root_path, _, log in cases:
            with self.subTest(root=root_path.name):
                with self.assertRaises(witness.WitnessError):
                    witness.verify_production_artifact(
                        root_path,
                        "preheldout_receipt",
                        self.artifact(),
                    )
                self.assertFalse(log.exists())
        with self.assertRaises(witness.WitnessError):
            witness.verify_production_artifact(
                {"scope": "production"},
                "preheldout_receipt",
                self.artifact(),
            )

    def test_script_requires_matching_interpreter_hash(self):
        witness = self.module()
        cases = (
            self.write_root(
                include_interpreter_sha256=False,
                script=True,
            ),
            self.write_root(
                interpreter_sha256=None,
                script=True,
            ),
            self.write_root(
                interpreter_sha256="0" * 64,
                script=True,
            ),
        )
        for root_path, _, log in cases:
            with self.subTest(root=root_path.name):
                with self.assertRaises(witness.WitnessError):
                    witness.verify_production_artifact(
                        root_path,
                        "preheldout_receipt",
                        self.artifact(),
                    )
                self.assertFalse(log.exists())

    def test_script_verifier_is_rejected_despite_matching_hashes(self):
        witness = self.module()
        root_path, _, log = self.write_root(script=True)
        with self.assertRaisesRegex(
            witness.WitnessError,
            "script production verifier is unsupported",
        ):
            witness.verify_production_artifact(
                root_path,
                "preheldout_receipt",
                self.artifact(),
            )
        self.assertFalse(log.exists())

    def test_native_verifier_requires_null_interpreter_hash(self):
        witness = self.module()
        native = pathlib.Path(sys.executable).resolve()
        root_path, _, _ = self.write_root(
            executable=native,
            interpreter_sha256="0" * 64,
        )
        with self.assertRaises(witness.WitnessError):
            witness.verify_production_artifact(
                root_path,
                "preheldout_receipt",
                self.artifact(),
            )
        root_path, _, _ = self.write_root(
            executable=native,
            interpreter_sha256=None,
        )
        sentinel = witness.WitnessError("exchange sentinel")
        with mock.patch.object(
            witness,
            "_bounded_exchange",
            side_effect=sentinel,
        ) as exchange:
            with self.assertRaisesRegex(
                witness.WitnessError, "exchange sentinel"
            ):
                witness.verify_production_artifact(
                    root_path,
                    "preheldout_receipt",
                    self.artifact(),
                )
        exchange.assert_called_once()

    def test_launcher_plus_mutable_script_is_rejected_before_launch(
        self,
    ):
        witness = self.module()
        marker = self.root / "launcher-side-effect"
        mutable_script = self.root / "mutable-verifier.py"
        mutable_script.write_text(
            (
                "import pathlib\n"
                f"pathlib.Path({str(marker)!r}).write_text("
                "'launched', encoding='utf-8')\n"
            ),
            encoding="utf-8",
        )
        launcher = pathlib.Path(sys.executable).resolve()
        root = {
            "schema_version": 1,
            "scope": "production",
            "trust_root_id": "prod-root",
            "verifier_protocol":
                "history-calibration-witness-v1",
            "verifier_argv": [
                str(launcher),
                str(mutable_script),
            ],
            "verifier_executable_sha256": hashlib.sha256(
                launcher.read_bytes()
            ).hexdigest(),
            "verifier_interpreter_sha256": None,
        }
        root_path = self.root / "launcher-root.json"
        root_path.write_bytes(canonical(root))
        with self.assertRaises(witness.WitnessError):
            witness.verify_production_artifact(
                root_path,
                "preheldout_receipt",
                self.artifact(),
            )
        self.assertFalse(marker.exists())

    def test_shebang_interpreter_is_direct_absolute_and_argument_free(
        self,
    ):
        witness = self.module()
        interpreter = pathlib.Path(sys.executable).resolve()
        for shebang in (
            "/usr/bin/env python3",
            f"{interpreter} -I",
            "python3",
        ):
            root_path, _, log = self.write_root(
                shebang=shebang
            )
            with self.subTest(shebang=shebang):
                with self.assertRaises(witness.WitnessError):
                    witness.verify_production_artifact(
                        root_path,
                        "preheldout_receipt",
                        self.artifact(),
                    )
                self.assertFalse(log.exists())

    def test_interpreter_path_is_canonical_single_link_regular(self):
        witness = self.module()
        interpreter = pathlib.Path(sys.executable).resolve()
        digest = hashlib.sha256(
            interpreter.read_bytes()
        ).hexdigest()
        alias = self.root / "interpreter-link"
        alias.symlink_to(interpreter)
        paths = (
            str(alias),
            f"{interpreter.parent}/./{interpreter.name}",
        )
        for path in paths:
            root_path, _, log = self.write_root(
                shebang=path,
                interpreter_sha256=digest,
            )
            with self.subTest(path=path):
                with self.assertRaises(witness.WitnessError):
                    witness.verify_production_artifact(
                        root_path,
                        "preheldout_receipt",
                        self.artifact(),
                    )
                self.assertFalse(log.exists())

    def test_verifier_path_must_be_canonical(self):
        witness = self.module()
        root_path, root, log = self.write_root()
        executable = pathlib.Path(root["verifier_argv"][0])
        noncanonical = (
            f"{executable.parent}/./{executable.name}"
        )
        root_path, _, log = self.write_root(
            executable=executable,
            argv0=noncanonical,
        )
        with self.assertRaisesRegex(
            witness.WitnessError,
            "verifier executable path is not canonical",
        ):
            witness.verify_production_artifact(
                root_path,
                "preheldout_receipt",
                self.artifact(),
            )
        self.assertFalse(log.exists())

    def test_executable_aliases_and_missing_execute_bit_are_rejected(self):
        witness = self.module()
        verifier = self.write_verifier(
            self.root / "alias-source",
            mode="ok",
            log=self.root / "alias-request.json",
        )
        symlink = self.root / "verifier-link"
        symlink.symlink_to(verifier)
        hardlink = self.root / "verifier-hardlink"
        os.link(verifier, hardlink)
        cases = []
        for executable in (symlink, hardlink):
            cases.append(self.write_root(executable=executable))
        for root_path, _, log in cases:
            with self.subTest(root=root_path.name):
                with self.assertRaises(witness.WitnessError):
                    witness.verify_production_artifact(
                        root_path,
                        "preheldout_receipt",
                        self.artifact(),
                    )
                self.assertFalse(log.exists())
        hardlink.unlink()
        verifier.chmod(0o600)
        root_path, _, log = self.write_root(
            executable=verifier
        )
        with self.assertRaises(witness.WitnessError):
            witness.verify_production_artifact(
                root_path,
                "preheldout_receipt",
                self.artifact(),
            )
        self.assertFalse(log.exists())

    def test_nonproduction_or_wrong_root_artifact_never_launches(self):
        witness = self.module()
        for artifact in (
            self.artifact(scope="synthetic_contract_only"),
            self.artifact(root_id="other-root"),
        ):
            root_path, _, log = self.write_root()
            with self.subTest(artifact=artifact):
                with self.assertRaises(witness.WitnessError):
                    witness.verify_production_artifact(
                        root_path,
                        "preheldout_receipt",
                        artifact,
                    )
                self.assertFalse(log.exists())

    def test_response_must_be_exact_canonical_positive_binding(self):
        witness = self.module()
        for mode in (
            "false",
            "wrong-request",
            "extra-field",
            "bool-version",
            "noncanonical",
            "trailing",
            "invalid-utf8",
            "nonzero",
        ):
            root_path, _, log = self.write_root(mode=mode)
            with self.subTest(mode=mode):
                with self.assertRaises(witness.WitnessError):
                    witness.verify_production_artifact(
                        root_path,
                        "calibration_capability",
                        self.artifact(),
                    )
                self.assertTrue(log.exists())

    def test_timeout_and_output_bounds_kill_the_verifier(self):
        witness = self.module()
        for mode in (
            "timeout",
            "oversized-stdout",
            "oversized-stderr",
        ):
            root_path, _, log = self.write_root(mode=mode)
            started = time.monotonic()
            with self.subTest(mode=mode):
                with mock.patch.object(
                    witness,
                    "PROCESS_TIMEOUT_SECONDS",
                    0.15 if mode == "timeout" else 5.0,
                ):
                    with self.assertRaises(witness.WitnessError):
                        witness.verify_production_artifact(
                            root_path,
                            "preheldout_receipt",
                            self.artifact(),
                        )
                maximum = (
                    1.5 if mode == "timeout" else 5.5
                )
                self.assertLess(
                    time.monotonic() - started, maximum
                )
                if mode != "timeout":
                    self.assertTrue(log.exists())

    def test_completed_verifier_descendants_are_killed(self):
        witness = self.module()
        log = self.root / "descendant-request.json"
        executable = self.write_script_verifier(
            self.root / "descendant-verifier",
            mode="spawn-descendant",
            log=log,
        )
        stdout, stderr = witness._bounded_exchange(
            [str(pathlib.Path(sys.executable).resolve()), str(executable)],
            b"{}\n",
            pathlib.Path(sys.executable).resolve(),
        )
        self.assertEqual(stdout, b"ok")
        self.assertEqual(stderr, b"")
        marker = pathlib.Path(str(log) + ".descendant")
        time.sleep(0.6)
        self.assertFalse(marker.exists())

    def test_postlaunch_hash_rejects_executable_mutation(self):
        witness = self.module()
        root_path, _, log = self.write_root(mode="mutate")
        with self.assertRaises(witness.WitnessError):
            witness.verify_production_artifact(
                root_path,
                "preheldout_receipt",
                self.artifact(),
            )
        self.assertTrue(log.exists())

    def test_rejected_script_cannot_mutate_interpreter(self):
        witness = self.module()
        interpreter = self.root / "mutable-interpreter"
        shutil.copyfile(
            pathlib.Path(sys.executable).resolve(),
            interpreter,
        )
        interpreter.chmod(0o700)
        before = interpreter.read_bytes()
        root_path, _, log = self.write_root(
            mode="mutate-interpreter",
            shebang=str(interpreter),
        )
        with self.assertRaisesRegex(
            witness.WitnessError,
            "script production verifier is unsupported",
        ):
            witness.verify_production_artifact(
                root_path,
                "preheldout_receipt",
                self.artifact(),
            )
        self.assertFalse(log.exists())
        self.assertEqual(interpreter.read_bytes(), before)

    def test_pinned_argv_is_literal_and_never_shell_parsed(self):
        witness = self.module()
        marker = (
            pathlib.Path("/tmp")
            / f"{self.root.name}-shell-side-effect"
        )
        executable_name = (
            pathlib.Path("fake-witness; touch ")
            / "tmp"
            / marker.name
        )
        root_path, _, log = self.write_root(
            executable_name=executable_name
        )
        witness.verify_production_artifact(
            root_path, "preheldout_receipt", self.artifact()
        )
        self.assertFalse(marker.exists())
        self.assertEqual(
            json.loads(
                pathlib.Path(str(log) + ".argv").read_text(
                    encoding="utf-8"
                )
            ),
            [],
        )

    def test_invalid_trust_root_id_type_is_normalized(self):
        witness = self.module()
        root_path, _, log = self.write_root(
            extra_fields={"trust_root_id": 1}
        )
        with self.assertRaises(witness.WitnessError):
            witness.verify_production_artifact(
                root_path,
                "preheldout_receipt",
                self.artifact(),
            )
        self.assertFalse(log.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
