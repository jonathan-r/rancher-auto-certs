#!/usr/bin/env python3

import datetime
import os
import subprocess
import uuid
import json
import logging
import sys
import time
import traceback

import requests
import yaml
import datadog

import libs.acme_tiny as acme_tiny


# Rancher env variables:
# - CATTLE_URL
# - CATTLE_ACCESS_KEY
# - CATTLE_SECRET_KEY
def rancher_get_certs():
    r = requests.get(os.environ['CATTLE_URL'] + "/certificates",
                     auth=(os.environ['CATTLE_ACCESS_KEY'], os.environ['CATTLE_SECRET_KEY']))
    if r.status_code != 200:
        raise Exception('Rancher returned non-200 code: ' + str(r.status_code) + ' - ' + r.text)
    return r.json()["data"]


def rancher_save_cert(name, private_key, cert, link=None):

    payload = {'key': private_key, 'cert': cert}

    if link is None:  # New certificate
        payload["name"] = name
        r = requests.post(os.environ['CATTLE_URL'] + "/certificates", data=json.dumps(payload),
                          headers={'Content-Type': 'application/json'},
                          auth=(os.environ['CATTLE_ACCESS_KEY'], os.environ['CATTLE_SECRET_KEY']))

    else:  # Update existing certificate
        r = requests.put(link, data=json.dumps(payload),
                         headers={'Content-Type': 'application/json'},
                         auth=(os.environ['CATTLE_ACCESS_KEY'], os.environ['CATTLE_SECRET_KEY']))

    if r.status_code not in [200, 201]:
        raise Exception('Rancher returned non-200 code: ' + str(r.status_code) + ' - ' + r.text)


def openssl(args, input=None):
    proc = subprocess.Popen(["openssl"] + args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    stdout, stderr = proc.communicate(input)
    if proc.returncode != 0:
        raise IOError("OpenSSL Error: {0}".format(stderr.decode("utf-8")))
    return stdout


def make_cert(config, logger, name, domains, link=None):
    logger.info("Creating certificate {0} for domains: {1}".format(name, ', '.join(domains)))

    if len(domains) < 1:
        raise Exception("No domains for certificate")

    private_key_file = "/tmp/" + uuid.uuid4().hex
    csr_file = "/tmp/" + uuid.uuid4().hex

    logger.debug("Generating private key to " + private_key_file + "...")
    openssl(["genrsa", "-out", private_key_file, str(config["key_length"])])

    with open(private_key_file, 'r') as f:
        private_key = f.read()

    logger.debug("Generating CSR to " + csr_file + "...")
    if len(domains) == 1:
        openssl(["req", "-new", "-sha256", "-key", private_key_file, "-out", csr_file, "-subj", "/CN=" + domains[0]])
    else:
        csr_config_file = "/tmp/" + uuid.uuid4().hex
        logger.debug("Generating CSR config to " + csr_config_file + "...")
        with open("/etc/ssl/openssl.cnf", "r") as f:
            openssl_config = f.read()
        openssl_config += "\n[SAN]\nsubjectAltName=DNS:" + ',DNS:'.join(domains) + "\n"
        with open(csr_config_file, "w") as f:
            f.write(openssl_config)
        openssl(["req", "-new", "-sha256", "-key", private_key_file, "-out", csr_file, "-subj", "/", "-reqexts", "SAN", "-config", csr_config_file])
        logger.debug("Deleting CSR config file...")
        os.remove(csr_config_file)

    logger.debug("Deleting private key file...")
    os.remove(private_key_file)

    logger.info("Signing CSR using acme_tiny...")
    tiny_kwargs = {}
    if "ca" in config:
        tiny_kwargs["CA"] = config["ca"]
    else:
        tiny_kwargs["directory_url"] = config["ca_directory"]
    cert = acme_tiny.get_crt(config["account_key"], csr_file, config["acme_dir"], log=logger, **tiny_kwargs)

    logger.debug("Deleting CSR file...")
    os.remove(csr_file)

    # TODO: Backup certificate & key ?

    logger.info("Saving cert in Rancher...")
    rancher_save_cert(name, private_key, cert, link)


def load_config(logger):

    with open("config/config.yml", "r") as f:
        config = yaml.safe_load(f)

    # Validation
    if "ca" in config and "ca_directory" in config:
        raise Exception("The config should have either ca_directory or ca (deprecated) but not both.")
    if "ca" in config:
        logger.warning("The config 'ca' is deprecated, please use 'ca_directory' instead.")
    if "chain" in config:
        logger.warning("The config 'chain' is not used anymore.")

    # Strip cert names and domains
    for cert in config["certs"]:
        cert["name"] = cert["name"].strip()
        for i in range(len(cert["domains"])):
            cert["domains"][i] = cert["domains"][i].strip()

    return config


def contains_sublist(lst, sublst):
    for e in sublst:
        if e not in lst:
            return False
    return True


def check_certs(config, logger):
    now = datetime.datetime.now()

    logger.info("Getting certificates from Rancher...")
    rancher_certs = rancher_get_certs()

    rancher_certs_by_name = {}
    for cert in rancher_certs:
        rancher_certs_by_name[cert["name"].strip()] = cert

    # Log which certs are in Rancher and in the config
    logger.debug("Found certs from Rancher:")
    for cert in rancher_certs:
        logger.debug("- " + cert["name"] + ": " + ', '.join(cert["subjectAlternativeNames"]))
    logger.debug("Found certs from config:")
    for cert in config["certs"]:
        logger.debug("- " + cert["name"] + ": " + ', '.join(cert["domains"]))

    to_do = []  # List of (remaining_days, name, domains, link) for certs to make

    logger.info("Checking certs:")
    for cert_config in config["certs"]:
        name = cert_config["name"]
        domains = cert_config["domains"]
        if name not in rancher_certs_by_name:
            logger.info("- Cert " + name + " does not exists")
            to_do.append((0, name, domains, None))
        else:
            rancher_cert = rancher_certs_by_name[name]
            link = rancher_cert["links"]["self"]
            if contains_sublist(rancher_cert["subjectAlternativeNames"], domains):
                cert_exp = datetime.datetime.strptime(rancher_cert["expiresAt"], "%a %b %d %H:%M:%S %Z %Y")
                remaining_days = (cert_exp - now).days
                logger.info("- Cert {0} expires in {1} days".format(name, remaining_days))
                if remaining_days < 30:
                    to_do.append((remaining_days, name, domains, link))
            else:
                logger.info("- Cert " + name + " is missing domains")
                to_do.append((0, name, domains, link))

    # Renew certs in the order they expire
    to_do.sort()
    for (_, name, domains, link) in to_do:
        make_cert(config, logger, name, domains, link)

    return len(to_do)


def setup_logging():
    # Configure the logger to send <= info messages to stdout and >= warning messages to stderr
    class InfoFilter(logging.Filter):
        def filter(self, rec):
            return rec.levelno in (logging.DEBUG, logging.INFO)
    logger = logging.getLogger(__name__)
    h1 = logging.StreamHandler(sys.stdout)
    h1.setLevel(logging.DEBUG)
    h1.addFilter(InfoFilter())
    h2 = logging.StreamHandler()
    h2.setLevel(logging.WARNING)
    logger.addHandler(h1)
    logger.addHandler(h2)
    # Configure logger level
    logger.setLevel(logging.DEBUG if ("LOG_DEBUG" in os.environ) else logging.INFO)


def single_run():
    logger = logging.getLogger(__name__)
    start_time = datetime.datetime.now()

    logger.info("*** Rancher Auto Certs started " + start_time.strftime("%Y-%m-%d %H:%M") + " ***")

    config = load_config(logger)
    logger.debug("Using CA %s and directory %s", config.get("ca"), config.get("ca_directory"))
    logger.debug("Using account key: " + config["account_key"])

    nb_certs = check_certs(config, logger)

    logger.info("*** {0} cert(s) created in {1} ***".format(nb_certs, datetime.datetime.now() - start_time))

    return nb_certs


def daemon():
    datadog.initialize(
            statsd_host=os.getenv("DOGSTATSD_HOST", "127.0.0.1"),
            statsd_port=int(os.getenv("DOGSTATSD_PORT", "8125")),
    )

    while True:
        try:
            nb_certs = single_run()
            datadog.statsd.event(
                    "Rancher Auto Certs executed successfully",
                    "{} certificate(s) created or renewed".format(nb_certs),
                    alert_type='success',
            )
            datadog.statsd.service_check('rancher_auto_certs.status', datadog.statsd.OK)
        except Exception as e:
            traceback.print_exc()
            datadog.statsd.event(
                    "Rancher Auto Certs encountered an error",
                    "Please check container logs.\n{}: {}".format(type(e).__name__, str(e)),
                    alert_type='error',
            )
            datadog.statsd.service_check('rancher_auto_certs.status', datadog.statsd.CRITICAL)
        time.sleep(24 * 60 * 60)


def main():
    setup_logging()
    if "--daemon" in sys.argv:
        daemon()
    else:
        single_run()


if __name__ == '__main__':
    main()
