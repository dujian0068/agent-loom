"""Verify the deployment script against real TLS hostname changes and failures."""

import http.server
import os
import ssl
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path


class DomainActivationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix="agentloom-domain-test-")
        cls.folder = Path(cls.directory.name)
        cls.hostname = "agentloom.bmilk.cn"
        cls.ca = cls.folder / "ca.pem"
        ca_key = cls.folder / "ca.key"
        ca_config = cls.folder / "ca.cnf"
        ca_config.write_text(
            "[req]\ndistinguished_name=dn\nx509_extensions=ca\nprompt=no\n"
            "[dn]\nCN=AgentLoom temporary test CA\n"
            "[ca]\nbasicConstraints=critical,CA:TRUE\n"
            "keyUsage=critical,keyCertSign,cRLSign\n"
        )
        cls.openssl(
            "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
            "-config", str(ca_config), "-keyout", str(ca_key), "-out", str(cls.ca),
        )
        cls.contexts = {}
        for label, hostname in [("old", "test-agentloom.bmilk.cn"), ("new", cls.hostname)]:
            key, csr, cert = [cls.folder / f"{label}.{suffix}" for suffix in ("key", "csr", "pem")]
            extension = cls.folder / f"{label}.ext"
            extension.write_text(
                f"subjectAltName=DNS:{hostname}\nbasicConstraints=critical,CA:FALSE\n"
                "keyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\n"
            )
            cls.openssl(
                "req", "-new", "-newkey", "rsa:2048", "-nodes", "-subj", f"/CN={hostname}",
                "-keyout", str(key), "-out", str(csr),
            )
            cls.openssl(
                "x509", "-req", "-in", str(csr), "-CA", str(cls.ca), "-CAkey", str(ca_key),
                "-CAcreateserial", "-days", "1", "-extfile", str(extension), "-out", str(cert),
            )
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(cert, key)
            cls.contexts[label] = context
            lineage = cls.folder / f"{label}-lineage"
            lineage.mkdir()
            (lineage / "cert.pem").write_bytes(cert.read_bytes())
            (lineage / "chain.pem").write_bytes(cls.ca.read_bytes())

    @classmethod
    def openssl(cls, *arguments):
        subprocess.run(["openssl", *arguments], check=True, capture_output=True, timeout=20)

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def setUp(self):
        self.ready = threading.Event()
        self.requests = 0
        current = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                current.requests += 1
                body = b'{"status":"ok"}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        def select_context(connection, _name, _context):
            connection.context = self.contexts["new" if self.ready.is_set() else "old"]

        self.contexts["old"].set_servername_callback(select_context)
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.socket = self.contexts["old"].wrap_socket(self.server.socket, server_side=True)
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def check_readiness(self):
        script = Path(__file__).resolve().parents[1] / "deploy/a2-activate-domain.sh"
        block = script.read_text().split("# BEGIN HTTPS READINESS CHECK\n", 1)[1]
        block = block.split("# END HTTPS READINESS CHECK", 1)[0]
        # Run the actual production check, substituting only an isolated port
        # and an explicit test trust anchor. Hostname verification stays enabled.
        block = block.replace("curl --noproxy", 'curl --cacert "$test_ca" --noproxy')
        block = block.replace("$domain:443:127.0.0.1", f"$domain:{self.port}:127.0.0.1")
        block = block.replace("https://$domain/api/", f"https://$domain:{self.port}/api/")
        return subprocess.run(
            ["bash", "-c", "set -Eeuo pipefail\n" + block],
            env={
                **os.environ, "domain": self.hostname, "test_ca": str(self.ca),
                "response_file": str(self.folder / f"health-{self.port}.json"),
            },
            text=True, capture_output=True, timeout=45, check=False,
        )

    def test_waits_for_new_certificate_after_reload(self):
        control = subprocess.run([
            "curl", "--noproxy", "*", "--cacert", str(self.ca), "--silent", "--show-error",
            "--max-time", "3", "--resolve", f"{self.hostname}:{self.port}:127.0.0.1",
            f"https://{self.hostname}:{self.port}/api/v1/health",
        ], text=True, capture_output=True, timeout=5, check=False)
        self.assertEqual(control.returncode, 60, control.stderr)
        timer = threading.Timer(0.75, self.ready.set)
        timer.start()
        try:
            result = self.check_readiness()
        finally:
            timer.cancel()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("certificate verification enabled", result.stdout)
        self.assertGreater(self.requests, 0)

    def test_wrong_certificate_stays_rejected_without_json_error(self):
        result = self.check_readiness()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("HTTPS did not become ready", result.stderr)
        self.assertNotIn("JSONDecodeError", result.stderr)
        self.assertNotIn("health check passed", result.stdout)
        self.assertEqual(self.requests, 0)

    def test_certificate_preflight_checks_hostname_and_chain(self):
        help_result = subprocess.run(
            ["openssl", "verify", "-help"], text=True, capture_output=True,
            timeout=5, check=False,
        )
        if "-verify_hostname" not in help_result.stdout + help_result.stderr:
            self.skipTest("Requires OpenSSL hostname verification; a2 and Linux CI use OpenSSL 3")
        script = Path(__file__).resolve().parents[1] / "deploy/a2-activate-domain.sh"
        block = script.read_text().split("# BEGIN CERTIFICATE VERIFICATION\n", 1)[1]
        block = block.split("# END CERTIFICATE VERIFICATION", 1)[0]
        block = block.replace("-CApath /etc/ssl/certs", '-CAfile "$test_ca"')
        block = block.replace("/etc/letsencrypt/live/$domain", "$test_lineage")
        for label in ("old", "new"):
            with self.subTest(certificate=label):
                result = subprocess.run(
                    ["bash", "-c", "set -Eeuo pipefail\n" + block],
                    env={
                        **os.environ, "domain": self.hostname, "test_ca": str(self.ca),
                        "test_lineage": str(self.folder / f"{label}-lineage"),
                    },
                    text=True, capture_output=True, timeout=10, check=False,
                )
                if label == "old":
                    self.assertNotEqual(result.returncode, 0, result.stdout)
                else:
                    self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
