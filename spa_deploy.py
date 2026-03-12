#!/usr/bin/env python3
"""Deploy a Vite/Yarn SPA project to S3, optionally fronted by CloudFront."""

import argparse
import datetime
import json
import mimetypes
import os
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

try:
    import boto3
    from botocore.exceptions import ClientError
except ImportError:
    sys.exit("boto3 is required. Install it with: pip install boto3")

STATE_FILE = "spa_deploy.json"


def load_state(project_dir: str) -> dict:
    path = os.path.join(project_dir, STATE_FILE)
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {"created_resources": []}


def save_state(project_dir: str, state: dict):
    path = os.path.join(project_dir, STATE_FILE)
    with open(path, "w") as f:
        json.dump(state, f, indent=2)
    print(f"State saved to {path}")


def detect_package_manager(project_dir: str) -> str:
    if os.path.exists(os.path.join(project_dir, "bun.lockb")) or os.path.exists(os.path.join(project_dir, "bun.lock")):
        return "bun"
    if os.path.exists(os.path.join(project_dir, "pnpm-lock.yaml")):
        return "pnpm"
    if os.path.exists(os.path.join(project_dir, "yarn.lock")):
        return "yarn"
    return "npm"


def run_build(project_dir: str):
    pm = detect_package_manager(project_dir)
    cmd = [pm, "run", "build"]
    print(f"Running: {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=project_dir)
    if result.returncode != 0:
        sys.exit(f"Build failed with exit code {result.returncode}")


def detect_output_dir(project_dir: str) -> str:
    for candidate in ["dist", "build"]:
        p = os.path.join(project_dir, candidate)
        if os.path.isdir(p):
            return p
    sys.exit("Could not detect build output directory. Use --output to specify it.")


def ensure_bucket(s3, bucket_name: str, region: str, state: dict, project_dir: str) -> bool:
    """Create bucket if it doesn't exist. Returns True if bucket was just created."""
    try:
        s3.head_bucket(Bucket=bucket_name)
        print(f"Bucket {bucket_name} already exists.")
        return False
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code not in ("404", "NoSuchBucket"):
            raise

    print(f"Creating bucket {bucket_name} in {region}...")
    params = {"Bucket": bucket_name}
    if region != "us-east-1":
        params["CreateBucketConfiguration"] = {"LocationConstraint": region}
    s3.create_bucket(**params)

    if "s3_bucket" not in state["created_resources"]:
        state["created_resources"].append("s3_bucket")
    state["bucket_name"] = bucket_name
    state["region"] = region
    save_state(project_dir, state)
    return True


def configure_website_hosting(s3, bucket_name: str, state: dict, project_dir: str):
    """Configure the bucket for static website hosting with public access."""
    print("Configuring S3 static website hosting...")

    # Disable block public access
    s3.put_public_access_block(
        Bucket=bucket_name,
        PublicAccessBlockConfiguration={
            "BlockPublicAcls": False,
            "IgnorePublicAcls": False,
            "BlockPublicPolicy": False,
            "RestrictPublicBuckets": False,
        },
    )

    # Set bucket policy for public read
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "PublicReadGetObject",
                "Effect": "Allow",
                "Principal": "*",
                "Action": "s3:GetObject",
                "Resource": f"arn:aws:s3:::{bucket_name}/*",
            }
        ],
    }
    s3.put_bucket_policy(Bucket=bucket_name, Policy=json.dumps(policy))

    # Enable website hosting
    s3.put_bucket_website(
        Bucket=bucket_name,
        WebsiteConfiguration={
            "IndexDocument": {"Suffix": "index.html"},
            "ErrorDocument": {"Key": "index.html"},
        },
    )

    state["s3_website_url"] = f"http://{bucket_name}.s3-website-{state['region']}.amazonaws.com"
    save_state(project_dir, state)


def upload_files(s3, bucket_name: str, output_dir: str):
    """Upload all files from the build output to S3, in parallel."""
    output_path = Path(output_dir)
    files = [f for f in output_path.rglob("*") if f.is_file()]
    print(f"Uploading {len(files)} files to s3://{bucket_name}/...")

    def upload_one(file_path: Path):
        rel = file_path.relative_to(output_path)
        key = rel.as_posix()
        content_type, _ = mimetypes.guess_type(str(file_path))
        if content_type is None:
            content_type = "application/octet-stream"

        extra_args = {"ContentType": content_type}

        # Set cache headers: long cache for hashed assets, short for html
        if file_path.suffix == ".html":
            extra_args["CacheControl"] = "no-cache"
        elif "assets" in rel.parts:
            extra_args["CacheControl"] = "public, max-age=31536000, immutable"

        s3.upload_file(str(file_path), bucket_name, key, ExtraArgs=extra_args)

    errors = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(upload_one, f): f for f in files}
        for future in as_completed(futures):
            if future.exception():
                errors.append((futures[future], future.exception()))

    if errors:
        for path, exc in errors:
            print(f"  Error uploading {path}: {exc}", file=sys.stderr)
        sys.exit(f"Upload failed: {len(errors)} file(s) could not be uploaded.")

    print("Upload complete.")


def find_hosted_zone(route53, domain: str) -> str:
    """Find the Route53 hosted zone ID for the given domain."""
    # Walk up the domain to find a matching zone (app.example.com -> example.com)
    parts = domain.split(".")
    for i in range(len(parts) - 1):
        candidate = ".".join(parts[i:])
        resp = route53.list_hosted_zones_by_name(DNSName=candidate, MaxItems="1")
        for zone in resp["HostedZones"]:
            zone_name = zone["Name"].rstrip(".")
            if zone_name == candidate:
                zone_id = zone["Id"].split("/")[-1]
                print(f"Found hosted zone: {zone_name} ({zone_id})")
                return zone_id
    sys.exit(f"No Route53 hosted zone found for {domain}")


def request_acm_certificate(session, domain: str, route53, zone_id: str, state: dict, project_dir: str, external_dns: bool = False) -> str:
    """Request an ACM certificate with DNS validation and wait for it to be issued."""
    # ACM must be in us-east-1 for CloudFront
    acm = session.client("acm", region_name="us-east-1")

    # Check if we already have a cert in state
    cert_arn = None
    if state.get("acm_certificate_arn"):
        arn = state["acm_certificate_arn"]
        try:
            resp = acm.describe_certificate(CertificateArn=arn)
            status = resp["Certificate"]["Status"]
            if status == "ISSUED":
                print(f"ACM certificate already issued: {arn}")
                return arn
            print(f"Existing certificate status: {status}, will wait for it...")
            cert_arn = arn  # Reuse; skip requesting a new one
        except ClientError:
            print("Previously tracked certificate not found, requesting new one...")

    if cert_arn is None:
        print(f"Requesting ACM certificate for {domain}...")
        cert_resp = acm.request_certificate(
            DomainName=domain,
            ValidationMethod="DNS",
        )
        cert_arn = cert_resp["CertificateArn"]
        state["acm_certificate_arn"] = cert_arn
        if "acm_certificate" not in state["created_resources"]:
            state["created_resources"].append("acm_certificate")
        save_state(project_dir, state)

    # Wait for DomainValidationOptions to appear
    print("Waiting for DNS validation details...")
    validation_record = None
    for _ in range(30):
        time.sleep(2)
        resp = acm.describe_certificate(CertificateArn=cert_arn)
        options = resp["Certificate"].get("DomainValidationOptions", [])
        if options and "ResourceRecord" in options[0]:
            validation_record = options[0]["ResourceRecord"]
            break

    if not validation_record:
        sys.exit("Timed out waiting for ACM validation details.")

    if external_dns:
        # Display instructions for manual DNS record creation
        print("\n" + "="*70)
        print("ACTION REQUIRED: Add the following CNAME record to your DNS provider")
        print("="*70)
        print(f"\nRecord Type: {validation_record['Type']}")
        print(f"Name:        {validation_record['Name']}")
        print(f"Value:       {validation_record['Value']}")
        print("\nNote: Remove any trailing dots if your DNS provider doesn't support them.")
        print("="*70)
        input("\nPress Enter once you've added the record and it has propagated...")
        print()
    else:
        # Create DNS validation record in Route53
        print(f"Creating validation record: {validation_record['Name']} -> {validation_record['Value']}")
        route53.change_resource_record_sets(
            HostedZoneId=zone_id,
            ChangeBatch={
                "Changes": [
                    {
                        "Action": "UPSERT",
                        "ResourceRecordSet": {
                            "Name": validation_record["Name"],
                            "Type": validation_record["Type"],
                            "TTL": 300,
                            "ResourceRecords": [{"Value": validation_record["Value"]}],
                        },
                    }
                ]
            },
        )
        if "route53_validation_record" not in state["created_resources"]:
            state["created_resources"].append("route53_validation_record")
        save_state(project_dir, state)

    # Wait for certificate to be issued
    print("Waiting for certificate validation (this may take a few minutes)...")
    for i in range(180):  # up to ~6 minutes (longer for external DNS)
        time.sleep(2)
        resp = acm.describe_certificate(CertificateArn=cert_arn)
        status = resp["Certificate"]["Status"]
        if status == "ISSUED":
            print("Certificate issued!")
            return cert_arn
        if status == "FAILED":
            sys.exit(f"Certificate validation failed: {resp['Certificate'].get('FailureReason')}")
        if i % 15 == 0 and i > 0:
            print(f"  Still waiting... (status: {status})")

    sys.exit("Timed out waiting for certificate to be issued.")


def create_domain_alias(route53, zone_id: str, domain: str, cf_domain: str, state: dict, project_dir: str):
    """Create a Route53 alias record pointing the domain to the CloudFront distribution."""
    print(f"Creating Route53 alias: {domain} -> {cf_domain}")
    route53.change_resource_record_sets(
        HostedZoneId=zone_id,
        ChangeBatch={
            "Changes": [
                {
                    "Action": "UPSERT",
                    "ResourceRecordSet": {
                        "Name": domain,
                        "Type": "A",
                        "AliasTarget": {
                            "HostedZoneId": "Z2FDTNDATAQYW2",  # CloudFront's fixed hosted zone ID
                            "DNSName": cf_domain,
                            "EvaluateTargetHealth": False,
                        },
                    },
                }
            ]
        },
    )
    if "route53_alias_record" not in state["created_resources"]:
        state["created_resources"].append("route53_alias_record")
    state["domain"] = domain
    state["route53_zone_id"] = zone_id
    save_state(project_dir, state)
    print(f"DNS alias created: {domain}")


def setup_cloudfront(session, bucket_name: str, region: str, state: dict, project_dir: str, domain: str = None):
    """Create CloudFront distribution with OAC fronting the S3 bucket."""
    cf = session.client("cloudfront")
    s3 = session.client("s3", region_name=region)

    # Remove website hosting config — CloudFront uses REST endpoint
    try:
        s3.delete_bucket_website(Bucket=bucket_name)
    except ClientError:
        pass

    # Block public access since CloudFront will be the only accessor
    s3.put_public_access_block(
        Bucket=bucket_name,
        PublicAccessBlockConfiguration={
            "BlockPublicAcls": True,
            "IgnorePublicAcls": True,
            "BlockPublicPolicy": False,  # we still need to put a policy
            "RestrictPublicBuckets": False,
        },
    )

    # Create or find existing OAC
    caller_ref = str(uuid.uuid4())
    oac_name = f"{bucket_name}-oac"
    oac_id = state.get("cloudfront_oac_id")

    if not oac_id:
        print("Creating Origin Access Control...")
        try:
            oac_resp = cf.create_origin_access_control(
                OriginAccessControlConfig={
                    "Name": oac_name,
                    "OriginAccessControlOriginType": "s3",
                    "SigningBehavior": "always",
                    "SigningProtocol": "sigv4",
                }
            )
            oac_id = oac_resp["OriginAccessControl"]["Id"]
        except cf.exceptions.OriginAccessControlAlreadyExists:
            print("OAC already exists, looking it up...")
            paginator = cf.get_paginator("list_origin_access_controls")
            for page in paginator.paginate():
                for item in page["OriginAccessControlList"].get("Items", []):
                    if item["Name"] == oac_name:
                        oac_id = item["Id"]
                        break
                if oac_id:
                    break
            if not oac_id:
                sys.exit(f"OAC '{oac_name}' exists but could not be found in listing.")
            print(f"Found existing OAC: {oac_id}")

        state["cloudfront_oac_id"] = oac_id
        save_state(project_dir, state)
    else:
        print(f"Using existing OAC: {oac_id}")

    s3_origin = f"{bucket_name}.s3.{region}.amazonaws.com"

    # Build distribution config
    dist_config = {
        "CallerReference": caller_ref,
        "Comment": f"SPA deploy: {bucket_name}",
        "Enabled": True,
        "DefaultRootObject": "index.html",
        "Origins": {
            "Quantity": 1,
            "Items": [
                {
                    "Id": "s3origin",
                    "DomainName": s3_origin,
                    "OriginAccessControlId": oac_id,
                    "S3OriginConfig": {"OriginAccessIdentity": ""},
                }
            ],
        },
        "DefaultCacheBehavior": {
            "TargetOriginId": "s3origin",
            "ViewerProtocolPolicy": "redirect-to-https",
            "AllowedMethods": {
                "Quantity": 2,
                "Items": ["GET", "HEAD"],
            },
            "CachePolicyId": "658327ea-f89d-4fab-a63d-7e88639e58f6",  # CachingOptimized
            "Compress": True,
        },
        "CustomErrorResponses": {
            "Quantity": 1,
            "Items": [
                {
                    "ErrorCode": 403,
                    "ResponsePagePath": "/index.html",
                    "ResponseCode": "200",
                    "ErrorCachingMinTTL": 10,
                }
            ],
        },
    }

    if domain and state.get("acm_certificate_arn"):
        dist_config["Aliases"] = {"Quantity": 1, "Items": [domain]}
        dist_config["ViewerCertificate"] = {
            "ACMCertificateArn": state["acm_certificate_arn"],
            "SSLSupportMethod": "sni-only",
            "MinimumProtocolVersion": "TLSv1.2_2021",
        }
    else:
        dist_config["ViewerCertificate"] = {"CloudFrontDefaultCertificate": True}

    print("Creating CloudFront distribution...")
    dist_resp = cf.create_distribution(DistributionConfig=dist_config)

    dist_id = dist_resp["Distribution"]["Id"]
    dist_domain = dist_resp["Distribution"]["DomainName"]

    # Set bucket policy allowing CloudFront
    dist_arn = dist_resp["Distribution"]["ARN"]
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "AllowCloudFrontServicePrincipal",
                "Effect": "Allow",
                "Principal": {"Service": "cloudfront.amazonaws.com"},
                "Action": "s3:GetObject",
                "Resource": f"arn:aws:s3:::{bucket_name}/*",
                "Condition": {"StringEquals": {"AWS:SourceArn": dist_arn}},
            }
        ],
    }
    s3.put_bucket_policy(Bucket=bucket_name, Policy=json.dumps(policy))

    if "cloudfront_distribution" not in state["created_resources"]:
        state["created_resources"].append("cloudfront_distribution")
    state["cloudfront_distribution_id"] = dist_id
    state["cloudfront_domain"] = dist_domain
    state["cloudfront_oac_id"] = oac_id
    save_state(project_dir, state)

    print(f"CloudFront distribution created: {dist_id}")
    print(f"Domain: https://{dist_domain}")
    print("Note: Distribution may take a few minutes to deploy globally.")


def print_status(state: dict):
    """Print a summary of the current deployment state."""
    if not state.get("created_resources"):
        print("No deployment state found.")
        return

    print("Deployment state:")
    if state.get("bucket_name"):
        print(f"  S3 bucket:    {state['bucket_name']} ({state.get('region', 'unknown region')})")
    if state.get("cloudfront_distribution_id"):
        print(f"  CloudFront:   {state['cloudfront_distribution_id']}")
    if state.get("acm_certificate_arn"):
        print(f"  ACM cert:     {state['acm_certificate_arn']}")
    if state.get("route53_zone_id"):
        print(f"  Route53 zone: {state['route53_zone_id']}")

    if state.get("domain"):
        print(f"\n  URL: https://{state['domain']}")
    elif state.get("cloudfront_domain"):
        print(f"\n  URL: https://{state['cloudfront_domain']}")
    elif state.get("s3_website_url"):
        print(f"\n  URL: {state['s3_website_url']}")


def destroy_all(session, state: dict, project_dir: str, yes: bool = False):
    """Destroy all resources tracked in the state file, in reverse dependency order."""
    resources = state.get("created_resources", [])
    if not resources:
        sys.exit("Nothing to destroy — no resources tracked in state file.")

    external_dns = state.get("external_dns", False)

    print("The following resources will be destroyed:")
    if "route53_alias_record" in resources and not external_dns:
        print(f"  - Route53 alias record: {state.get('domain')}")
    if "route53_validation_record" in resources and not external_dns:
        print(f"  - Route53 ACM validation record")
    if "cloudfront_distribution" in resources:
        print(f"  - CloudFront distribution: {state.get('cloudfront_distribution_id')}")
    if "acm_certificate" in resources:
        print(f"  - ACM certificate: {state.get('acm_certificate_arn')}")
    if "s3_bucket" in resources:
        print(f"  - S3 bucket: {state.get('bucket_name')} (all objects will be deleted)")
    
    if external_dns and state.get("domain"):
        print(f"\nNote: You'll need to manually remove DNS records for {state['domain']} from your DNS provider.")

    if yes:
        print("Auto-confirmed via --yes.")
    else:
        answer = input("\nAre you sure you want to destroy all resources? This cannot be undone. [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            sys.exit("Aborted.")

    region = state.get("region", "us-east-1")

    # 1. Delete Route53 alias record (skip if external DNS)
    if "route53_alias_record" in resources and state.get("domain") and state.get("route53_zone_id") and not external_dns:
        print(f"\nDeleting Route53 alias record: {state['domain']}...")
        route53 = session.client("route53")
        try:
            route53.change_resource_record_sets(
                HostedZoneId=state["route53_zone_id"],
                ChangeBatch={
                    "Changes": [
                        {
                            "Action": "DELETE",
                            "ResourceRecordSet": {
                                "Name": state["domain"],
                                "Type": "A",
                                "AliasTarget": {
                                    "HostedZoneId": "Z2FDTNDATAQYW2",
                                    "DNSName": state["cloudfront_domain"],
                                    "EvaluateTargetHealth": False,
                                },
                            },
                        }
                    ]
                },
            )
            print("  Deleted.")
        except ClientError as e:
            print(f"  Warning: {e}")

    # 2. Delete Route53 ACM validation record (skip if external DNS)
    if "route53_validation_record" in resources and state.get("acm_certificate_arn") and state.get("route53_zone_id") and not external_dns:
        print("Deleting Route53 ACM validation record...")
        acm = session.client("acm", region_name="us-east-1")
        route53 = session.client("route53")
        try:
            resp = acm.describe_certificate(CertificateArn=state["acm_certificate_arn"])
            options = resp["Certificate"].get("DomainValidationOptions", [])
            if options and "ResourceRecord" in options[0]:
                rec = options[0]["ResourceRecord"]
                route53.change_resource_record_sets(
                    HostedZoneId=state["route53_zone_id"],
                    ChangeBatch={
                        "Changes": [
                            {
                                "Action": "DELETE",
                                "ResourceRecordSet": {
                                    "Name": rec["Name"],
                                    "Type": rec["Type"],
                                    "TTL": 300,
                                    "ResourceRecords": [{"Value": rec["Value"]}],
                                },
                            }
                        ]
                    },
                )
                print("  Deleted.")
        except ClientError as e:
            print(f"  Warning: {e}")

    # 3. Disable and delete CloudFront distribution
    if "cloudfront_distribution" in resources and state.get("cloudfront_distribution_id"):
        dist_id = state["cloudfront_distribution_id"]
        cf = session.client("cloudfront")

        print(f"Disabling CloudFront distribution {dist_id}...")
        try:
            resp = cf.get_distribution(Id=dist_id)
            etag = resp["ETag"]
            config = resp["Distribution"]["DistributionConfig"]

            if config["Enabled"]:
                config["Enabled"] = False
                update_resp = cf.update_distribution(Id=dist_id, DistributionConfig=config, IfMatch=etag)
                etag = update_resp["ETag"]
                print("  Disabled. Waiting for distribution to deploy (this may take several minutes)...")
                waiter = cf.get_waiter("distribution_deployed")
                waiter.wait(Id=dist_id, WaiterConfig={"Delay": 15, "MaxAttempts": 60})

            print(f"Deleting CloudFront distribution {dist_id}...")
            cf.delete_distribution(Id=dist_id, IfMatch=etag)
            print("  Deleted.")
        except ClientError as e:
            print(f"  Warning: {e}")

        # Delete OAC
        if state.get("cloudfront_oac_id"):
            print(f"Deleting Origin Access Control {state['cloudfront_oac_id']}...")
            try:
                oac_resp = cf.get_origin_access_control(Id=state["cloudfront_oac_id"])
                cf.delete_origin_access_control(Id=state["cloudfront_oac_id"], IfMatch=oac_resp["ETag"])
                print("  Deleted.")
            except ClientError as e:
                print(f"  Warning: {e}")

    # 4. Delete ACM certificate
    if "acm_certificate" in resources and state.get("acm_certificate_arn"):
        print(f"Deleting ACM certificate {state['acm_certificate_arn']}...")
        acm = session.client("acm", region_name="us-east-1")
        try:
            acm.delete_certificate(CertificateArn=state["acm_certificate_arn"])
            print("  Deleted.")
        except ClientError as e:
            print(f"  Warning: {e}")

    # 5. Empty and delete S3 bucket
    if "s3_bucket" in resources and state.get("bucket_name"):
        bucket_name = state["bucket_name"]
        print(f"Emptying S3 bucket {bucket_name}...")
        s3 = session.resource("s3", region_name=region)
        try:
            bucket = s3.Bucket(bucket_name)
            bucket.object_versions.all().delete()
            bucket.objects.all().delete()
            print(f"Deleting S3 bucket {bucket_name}...")
            bucket.delete()
            print("  Deleted.")
        except ClientError as e:
            print(f"  Warning: {e}")

    # Remove state file
    state_path = os.path.join(project_dir, STATE_FILE)
    if os.path.exists(state_path):
        os.remove(state_path)
        print(f"\nState file removed: {state_path}")

    print("All resources destroyed.")


def preflight_check(session, state: dict, output_dir=None) -> list:
    """Run pre-flight checks in parallel. Returns list of issue dicts."""
    issues = []

    def _check_credentials():
        try:
            sts = session.client("sts")
            identity = sts.get_caller_identity()
            return [{"level": "info", "resource": "credentials",
                     "message": f"Authenticated as {identity['Arn']}"}]
        except Exception as e:
            return [{"level": "error", "resource": "credentials",
                     "message": f"AWS credentials invalid: {e}"}]

    def _check_s3():
        bucket = state.get("bucket_name")
        if not bucket:
            return []
        try:
            s3 = session.client("s3")
            s3.head_bucket(Bucket=bucket)
            return []
        except ClientError as e:
            code = e.response["Error"]["Code"]
            if code in ("404", "NoSuchBucket"):
                return [{"level": "error", "resource": "s3",
                         "message": f"Bucket {bucket!r} does not exist"}]
            elif code == "403":
                return [{"level": "error", "resource": "s3",
                         "message": f"Access denied to bucket {bucket!r}"}]
            return [{"level": "error", "resource": "s3", "message": str(e)}]

    def _check_cloudfront():
        dist_id = state.get("cloudfront_distribution_id")
        if not dist_id:
            return []
        result = []
        try:
            cf = session.client("cloudfront")
            resp = cf.get_distribution(Id=dist_id)
            dist = resp["Distribution"]
            config = dist["DistributionConfig"]
            if not config.get("Enabled"):
                result.append({"level": "error", "resource": "cloudfront",
                                "message": f"Distribution {dist_id} is disabled"})
            if dist.get("Status") != "Deployed":
                result.append({"level": "warning", "resource": "cloudfront",
                                "message": f"Distribution {dist_id} status is {dist.get('Status')!r} (not Deployed)"})
            # Check OAC
            origins = config.get("Origins", {}).get("Items", [])
            has_oac = any(o.get("OriginAccessControlId") for o in origins)
            if not has_oac:
                result.append({"level": "warning", "resource": "cloudfront",
                                "message": f"Distribution {dist_id} has no OAC configured"})
            # Check cloudfront_domain match
            actual_domain = dist.get("DomainName", "")
            state_domain = state.get("cloudfront_domain", "")
            if state_domain and actual_domain and actual_domain != state_domain:
                result.append({"level": "warning", "resource": "cloudfront",
                                "message": f"cloudfront_domain in state ({state_domain!r}) does not match actual ({actual_domain!r})"})
            # Check alias
            custom_domain = state.get("domain")
            if custom_domain:
                aliases = config.get("Aliases", {}).get("Items", [])
                if custom_domain not in aliases:
                    result.append({"level": "warning", "resource": "cloudfront",
                                   "message": f"Domain {custom_domain!r} not in distribution aliases"})
        except ClientError as e:
            code = e.response["Error"]["Code"]
            if code == "NoSuchDistribution":
                result.append({"level": "error", "resource": "cloudfront",
                               "message": f"Distribution {dist_id} does not exist"})
            else:
                result.append({"level": "error", "resource": "cloudfront", "message": str(e)})
        return result

    def _check_acm():
        cert_arn = state.get("acm_certificate_arn")
        if not cert_arn:
            return []
        result = []
        try:
            acm = session.client("acm", region_name="us-east-1")
            resp = acm.describe_certificate(CertificateArn=cert_arn)
            cert = resp["Certificate"]
            status = cert.get("Status")
            if status != "ISSUED":
                result.append({"level": "error", "resource": "acm",
                               "message": f"Certificate status is {status!r} (expected ISSUED)"})
            not_after = cert.get("NotAfter")
            if not_after:
                days_left = (not_after.replace(tzinfo=None) - datetime.datetime.utcnow()).days
                if days_left < 7:
                    result.append({"level": "error", "resource": "acm",
                                   "message": f"Certificate expires in {days_left} days — renew immediately"})
                elif days_left < 30:
                    result.append({"level": "warning", "resource": "acm",
                                   "message": f"Certificate expires in {days_left} days"})
        except ClientError as e:
            result.append({"level": "error", "resource": "acm", "message": str(e)})
        return result

    def _check_route53():
        zone_id = state.get("route53_zone_id")
        domain = state.get("domain")
        cf_domain = state.get("cloudfront_domain")
        if not zone_id or not domain:
            return []
        result = []
        try:
            route53 = session.client("route53")
            resp = route53.list_resource_record_sets(
                HostedZoneId=zone_id,
                StartRecordName=domain,
                StartRecordType="A",
                MaxItems="1",
            )
            records = resp.get("ResourceRecordSets", [])
            found = any(
                r["Name"].rstrip(".") == domain.rstrip(".") and r["Type"] == "A"
                for r in records
            )
            if not found:
                result.append({"level": "error", "resource": "route53",
                               "message": f"No A record found for {domain!r} in zone {zone_id}"})
            elif cf_domain:
                for r in records:
                    if r["Name"].rstrip(".") == domain.rstrip(".") and r["Type"] == "A":
                        alias_target = r.get("AliasTarget", {}).get("DNSName", "").rstrip(".")
                        if alias_target and alias_target != cf_domain.rstrip("."):
                            result.append({"level": "warning", "resource": "route53",
                                           "message": f"A record alias target {alias_target!r} does not match cloudfront_domain {cf_domain!r}"})
        except ClientError as e:
            result.append({"level": "error", "resource": "route53", "message": str(e)})
        return result

    def _check_oac():
        oac_id = state.get("cloudfront_oac_id")
        if not oac_id:
            return []
        try:
            cf = session.client("cloudfront")
            cf.get_origin_access_control(Id=oac_id)
            return []
        except ClientError as e:
            code = e.response["Error"]["Code"]
            if code == "NoSuchOriginAccessControl":
                return [{"level": "error", "resource": "oac",
                         "message": f"Origin Access Control {oac_id} does not exist"}]
            return [{"level": "error", "resource": "oac", "message": str(e)}]

    def _check_output_dir():
        if not output_dir:
            return []
        result = []
        p = Path(output_dir)
        if not p.is_dir() or not any(p.iterdir()):
            result.append({"level": "warning", "resource": "output_dir",
                           "message": f"Output directory {output_dir!r} is empty or missing"})
        elif not (p / "index.html").exists():
            result.append({"level": "warning", "resource": "output_dir",
                           "message": f"index.html not found in {output_dir!r}"})
        return result

    # Determine which checks to run
    checks = [_check_credentials, _check_output_dir]
    if state.get("bucket_name"):
        checks.append(_check_s3)
    if state.get("cloudfront_distribution_id"):
        checks.append(_check_cloudfront)
    if state.get("cloudfront_oac_id"):
        checks.append(_check_oac)
    if state.get("acm_certificate_arn"):
        checks.append(_check_acm)
    # Only check Route53 if not using external DNS
    if state.get("route53_zone_id") and state.get("domain") and not state.get("external_dns"):
        checks.append(_check_route53)

    with ThreadPoolExecutor(max_workers=len(checks)) as executor:
        futures = [executor.submit(fn) for fn in checks]
        for future in as_completed(futures):
            try:
                issues.extend(future.result())
            except Exception as e:
                issues.append({"level": "error", "resource": "unknown",
                               "message": f"Check failed with exception: {e}"})

    return issues


def print_preflight_results(issues: list) -> bool:
    """Print preflight check results. Returns True if no errors."""
    print("Pre-flight checks:")
    for issue in issues:
        level = issue["level"]
        resource = issue["resource"]
        message = issue["message"]
        if level == "info":
            tag = "[ok]  "
        elif level == "warning":
            tag = "[warn]"
        else:
            tag = "[error]"
        print(f"  {tag} {resource}: {message}")

    errors = sum(1 for i in issues if i["level"] == "error")
    warnings = sum(1 for i in issues if i["level"] == "warning")

    if errors or warnings:
        parts = []
        if errors:
            parts.append(f"{errors} error{'s' if errors != 1 else ''}")
        if warnings:
            parts.append(f"{warnings} warning{'s' if warnings != 1 else ''}")
        suffix = " — fix errors before deploying." if errors else ""
        print(f"\n{', '.join(parts)}{suffix}")

    return errors == 0


def discover_deployments(session) -> list:
    """Scan the AWS account for existing SPA deployments."""
    deployments = []
    cf_buckets = set()

    # Step 1: CloudFront distributions
    cf = session.client("cloudfront")
    paginator = cf.get_paginator("list_distributions")
    for page in paginator.paginate():
        dist_list = page.get("DistributionList", {})
        for dist in dist_list.get("Items", []):
            origins = dist.get("Origins", {}).get("Items", [])
            s3_origins = [o for o in origins if ".s3." in o.get("DomainName", "") and "amazonaws.com" in o.get("DomainName", "")]
            if not s3_origins:
                continue

            origin = s3_origins[0]
            origin_domain = origin["DomainName"]
            # Extract bucket name from origin domain like bucket.s3.region.amazonaws.com
            bucket_name = origin_domain.split(".s3.")[0]
            cf_buckets.add(bucket_name)

            aliases_list = dist.get("Aliases", {}).get("Items", [])
            cert = dist.get("ViewerCertificate", {})
            acm_arn = cert.get("ACMCertificateArn") if cert.get("ACMCertificateArn") else None
            oac_id = origin.get("OriginAccessControlId") or None

            url = f"https://{aliases_list[0]}" if aliases_list else f"https://{dist['DomainName']}"

            deployments.append({
                "type": "cloudfront",
                "cloudfront_distribution_id": dist["Id"],
                "cloudfront_domain": dist["DomainName"],
                "bucket_name": bucket_name,
                "origin_domain": origin_domain,
                "enabled": dist.get("Enabled", False),
                "status": dist.get("Status", "Unknown"),
                "aliases": aliases_list,
                "default_root_object": dist.get("DefaultRootObject", ""),
                "acm_certificate_arn": acm_arn,
                "oac_id": oac_id,
                "url": url,
            })

    # Step 2: S3 website-only buckets
    s3_global = session.client("s3")
    buckets_resp = s3_global.list_buckets()
    all_buckets = [b["Name"] for b in buckets_resp.get("Buckets", [])
                   if b["Name"] not in cf_buckets]

    def _check_s3_website(bucket_name):
        try:
            loc_resp = s3_global.get_bucket_location(Bucket=bucket_name)
            region = loc_resp.get("LocationConstraint") or "us-east-1"
            s3_regional = session.client("s3", region_name=region)
            s3_regional.get_bucket_website(Bucket=bucket_name)
            url = f"http://{bucket_name}.s3-website-{region}.amazonaws.com"
            return {"type": "s3", "bucket_name": bucket_name, "region": region, "url": url}
        except ClientError:
            return None

    with ThreadPoolExecutor(max_workers=20) as executor:
        futures = {executor.submit(_check_s3_website, b): b for b in all_buckets}
        for future in as_completed(futures):
            result = future.result()
            if result:
                deployments.append(result)

    return deployments


def print_discovery_results(deployments: list):
    """Print a numbered table of discovered SPA deployments."""
    count = len(deployments)
    print(f"Found {count} SPA deployment{'s' if count != 1 else ''}:\n")
    for i, d in enumerate(deployments, 1):
        if d["type"] == "cloudfront":
            has_domain = bool(d.get("aliases"))
            label = "CloudFront + custom domain" if has_domain else "CloudFront (no custom domain)"
            enabled_str = "enabled" if d.get("enabled") else "disabled"
            status_str = d.get("status", "Unknown")
            print(f"  {i}. {label}  [{status_str}, {enabled_str}]")
            print(f"     Bucket:       {d['bucket_name']}")
            print(f"     Distribution: {d['cloudfront_distribution_id']}")
            if has_domain:
                print(f"     Aliases:      {', '.join(d['aliases'])}")
            print(f"     URL:          {d['url']}")
        else:
            print(f"  {i}. S3 static website")
            print(f"     Bucket:       {d['bucket_name']} ({d.get('region', 'unknown')})")
            print(f"     URL:          {d['url']}")
        print()


def import_deployment(session, deployment: dict, project_dir: str, state: dict) -> dict:
    """Import a discovered deployment into spa_deploy.json."""
    new_state = {"created_resources": []}

    if deployment["type"] == "cloudfront":
        bucket_name = deployment["bucket_name"]
        # Get bucket region
        s3 = session.client("s3")
        try:
            loc = s3.get_bucket_location(Bucket=bucket_name)
            region = loc.get("LocationConstraint") or "us-east-1"
        except ClientError:
            region = "us-east-1"

        new_state["bucket_name"] = bucket_name
        new_state["region"] = region
        new_state["cloudfront_distribution_id"] = deployment["cloudfront_distribution_id"]
        new_state["cloudfront_domain"] = deployment["cloudfront_domain"]
        new_state["created_resources"].append("s3_bucket")
        new_state["created_resources"].append("cloudfront_distribution")

        if deployment.get("oac_id"):
            new_state["cloudfront_oac_id"] = deployment["oac_id"]

        if deployment.get("acm_certificate_arn"):
            new_state["acm_certificate_arn"] = deployment["acm_certificate_arn"]
            new_state["created_resources"].append("acm_certificate")
            new_state["created_resources"].append("route53_validation_record")

        if deployment.get("aliases"):
            domain = deployment["aliases"][0]
            new_state["domain"] = domain
            new_state["created_resources"].append("route53_alias_record")

            # Try to find Route53 zone
            try:
                route53 = session.client("route53")
                zone_id = find_hosted_zone(route53, domain)
                new_state["route53_zone_id"] = zone_id
            except SystemExit:
                pass  # Zone not found — silently skip

    else:  # s3
        new_state["bucket_name"] = deployment["bucket_name"]
        new_state["region"] = deployment.get("region", "us-east-1")
        new_state["s3_website_url"] = deployment["url"]
        new_state["created_resources"].append("s3_bucket")

    save_state(project_dir, new_state)
    print(f"Imported deployment for bucket {new_state['bucket_name']!r} into {STATE_FILE}")
    return new_state


def main():
    parser = argparse.ArgumentParser(
        description="Build and deploy a Vite or Yarn SPA project to AWS S3, with optional CloudFront CDN, custom domain, ACM TLS certificate, and Route53 DNS configuration.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  %(prog)s --bucket my-app
      Build the project and deploy to S3 as a public static website.

  %(prog)s --bucket my-app --cloudfront
      Deploy to S3 fronted by a CloudFront distribution (HTTPS, global CDN).

  %(prog)s --bucket my-app --cloudfront --domain app.example.com
      Deploy with CloudFront, provision an ACM TLS certificate for the domain,
      and create a Route53 alias record pointing to the distribution.

  %(prog)s --bucket my-app --cloudfront --domain app.example.com --squarespace
      Deploy with CloudFront and custom domain using external DNS (e.g., Squarespace).
      The script will pause and display DNS records for you to add manually.

  %(prog)s --bucket my-app --skip-build --output ./dist
      Deploy a pre-built project from the ./dist directory.

  %(prog)s --bucket my-app --destroy
      Tear down all AWS resources tracked in the state file.

  %(prog)s
      Redeploy using settings from the existing spa_deploy.json state file.
      Builds, uploads, and invalidates the CloudFront cache. No flags needed.

state tracking:
  All created resources are recorded in a spa_deploy.json file in the project
  directory. On subsequent deploys, existing resources are reused — only new
  files are uploaded and the CloudFront cache is invalidated. The --destroy
  flag reads this file to determine what to tear down.""",
    )
    parser.add_argument(
        "--bucket",
        help="S3 bucket name. The bucket is created if it does not exist. Used as the origin for CloudFront when --cloudfront is set. On redeployment, this is read from the state file if not provided.",
    )
    parser.add_argument(
        "--cloudfront", action="store_true",
        help="Front the S3 bucket with a CloudFront distribution. The bucket is made private and served exclusively through CloudFront via Origin Access Control (OAC). Enables HTTPS and global edge caching. On redeployment, a /* cache invalidation is issued automatically.",
    )
    parser.add_argument(
        "--domain",
        help="Custom domain name (e.g. app.example.com). Requires --cloudfront. The script will: (1) find the matching Route53 hosted zone, (2) request a DNS-validated ACM certificate in us-east-1, (3) create the validation CNAME in Route53 and wait for issuance, (4) attach the certificate to the CloudFront distribution, and (5) create a Route53 A-record alias pointing the domain to CloudFront. The hosted zone must already exist in Route53.",
    )
    parser.add_argument(
        "--squarespace", action="store_true",
        help="Use external DNS (e.g., Squarespace) instead of Route53. When combined with --domain, the script will pause and display DNS records for you to add manually. Skips Route53 hosted zone lookup and alias record creation.",
    )
    parser.add_argument(
        "--region", default="us-east-1",
        help="AWS region for the S3 bucket (default: us-east-1). Note: ACM certificates for CloudFront are always created in us-east-1 regardless of this setting.",
    )
    parser.add_argument(
        "--dir", default=".",
        help="Path to the project directory (default: current directory). The script detects the package manager (yarn if yarn.lock exists, otherwise npm) and runs 'build' from this directory.",
    )
    parser.add_argument(
        "--output",
        help="Path to the build output directory. If not specified, the script auto-detects by looking for a dist/ or build/ directory inside the project directory.",
    )
    parser.add_argument(
        "--skip-build", action="store_true",
        help="Skip the build step and deploy the existing output directory as-is. Useful for CI pipelines where the build is handled separately.",
    )
    parser.add_argument(
        "--destroy", action="store_true",
        help="Destroy all AWS resources tracked in spa_deploy.json. Resources are removed in reverse dependency order: Route53 records, CloudFront distribution (disabled then deleted), OAC, ACM certificate, and S3 bucket (emptied then deleted). Prompts for confirmation before proceeding. The state file is removed after successful teardown.",
    )
    parser.add_argument(
        "--status", action="store_true",
        help="Print the current deployment state from spa_deploy.json and exit.",
    )
    parser.add_argument(
        "--yes", "-y", action="store_true",
        help="Skip all interactive confirmation prompts. Useful for CI/CD pipelines.",
    )
    parser.add_argument(
        "--profile",
        help="AWS profile name to use (from ~/.aws/credentials). Overrides the default profile.",
    )
    parser.add_argument(
        "--discover", action="store_true",
        help="Scan the AWS account for existing SPA deployments (CloudFront+S3 or S3 website-only) and display them.",
    )
    parser.add_argument(
        "--import", dest="import_index", type=int, metavar="N",
        help="Import deployment #N from --discover into spa_deploy.json so it can be managed going forward.",
    )
    args = parser.parse_args()

    project_dir = os.path.abspath(args.dir)
    if not os.path.isdir(project_dir):
        sys.exit(f"Project directory not found: {project_dir}")

    state = load_state(project_dir)

    if args.status:
        print_status(state)
        return

    if args.discover or args.import_index is not None:
        session = boto3.Session(profile_name=args.profile or None)
        deployments = discover_deployments(session)
        if not deployments:
            print("No SPA deployments found.")
            return
        print_discovery_results(deployments)

        if args.import_index is not None:
            idx = args.import_index - 1
            if idx < 0 or idx >= len(deployments):
                sys.exit(f"Invalid index {args.import_index}: choose 1–{len(deployments)}")
            import_deployment(session, deployments[idx], project_dir, state)
        elif not args.yes:
            answer = input("\nImport a deployment? Enter number (or press Enter to skip): ").strip()
            if answer.isdigit():
                idx = int(answer) - 1
                if 0 <= idx < len(deployments):
                    import_deployment(session, deployments[idx], project_dir, state)
        return

    # Fill in missing args from state file
    if not args.bucket:
        if state.get("bucket_name"):
            args.bucket = state["bucket_name"]
            print(f"Using bucket from state file: {args.bucket}")
        else:
            parser.error("--bucket is required (no existing state file found)")

    if not args.cloudfront and state.get("cloudfront_distribution_id"):
        args.cloudfront = True
        print("CloudFront mode enabled (detected from state file)")

    if not args.domain and state.get("domain"):
        args.domain = state["domain"]
        print(f"Using domain from state file: {args.domain}")

    if args.region == "us-east-1" and state.get("region"):
        args.region = state["region"]

    if args.domain and not args.cloudfront:
        parser.error("--domain requires --cloudfront")
    
    if args.squarespace and not args.domain:
        parser.error("--squarespace requires --domain")

    session = boto3.Session(region_name=args.region, profile_name=args.profile or None)

    # Destroy mode
    if args.destroy:
        print("\nRunning pre-flight checks...")
        issues = preflight_check(session, state)
        print_preflight_results(issues)
        destroy_all(session, state, project_dir, yes=args.yes)
        return

    # Build
    if not args.skip_build:
        run_build(project_dir)

    # Detect output
    output_dir = args.output if args.output else detect_output_dir(project_dir)
    if not os.path.isdir(output_dir):
        sys.exit(f"Output directory not found: {output_dir}")

    # Pre-flight checks
    print("\nRunning pre-flight checks...")
    issues = preflight_check(session, state, output_dir if not args.skip_build else None)
    if not print_preflight_results(issues):
        sys.exit("Pre-flight checks failed. Fix errors above before deploying.")

    # AWS session (already created above)
    s3 = session.client("s3", region_name=args.region)

    # S3 bucket
    ensure_bucket(s3, args.bucket, args.region, state, project_dir)

    # Upload
    upload_files(s3, args.bucket, output_dir)

    if args.cloudfront:
        # Check if distribution already exists in state
        if state.get("cloudfront_distribution_id"):
            print(f"CloudFront distribution already exists: {state['cloudfront_distribution_id']}")
            print(f"Domain: https://{state['cloudfront_domain']}")
            print("Skipping CloudFront creation. Files have been updated in S3.")
            # Optionally invalidate cache
            cf = session.client("cloudfront")
            print("Creating cache invalidation...")
            cf.create_invalidation(
                DistributionId=state["cloudfront_distribution_id"],
                InvalidationBatch={
                    "Paths": {"Quantity": 1, "Items": ["/*"]},
                    "CallerReference": str(uuid.uuid4()),
                },
            )
            print("Invalidation created.")
        else:
            if args.yes:
                confirmed = True
            else:
                answer = input("\nCreate a CloudFront distribution to front this S3 bucket? [y/N] ")
                confirmed = answer.strip().lower() in ("y", "yes")
            if confirmed:
                # Handle domain + ACM + Route53 if requested
                if args.domain:
                    if args.squarespace:
                        # External DNS mode - no Route53 operations
                        request_acm_certificate(session, args.domain, None, None, state, project_dir, external_dns=True)
                        state["external_dns"] = True
                    else:
                        # Route53 mode
                        route53 = session.client("route53")
                        zone_id = state.get("route53_zone_id") or find_hosted_zone(route53, args.domain)
                        request_acm_certificate(session, args.domain, route53, zone_id, state, project_dir, external_dns=False)
                    # Reload state after cert is saved
                    state = load_state(project_dir)

                setup_cloudfront(session, args.bucket, args.region, state, project_dir, domain=args.domain)

                # Create Route53 alias after distribution is created (only if not using external DNS)
                if args.domain and not args.squarespace:
                    state = load_state(project_dir)
                    create_domain_alias(route53, zone_id, args.domain, state["cloudfront_domain"], state, project_dir)
                elif args.domain and args.squarespace:
                    # Display instructions for creating CNAME to CloudFront
                    state = load_state(project_dir)
                    print("\n" + "="*70)
                    print("ACTION REQUIRED: Add the following CNAME record to your DNS provider")
                    print("="*70)
                    print(f"\nRecord Type: CNAME")
                    print(f"Name:        {args.domain}")
                    print(f"Value:       {state['cloudfront_domain']}")
                    print("\nNote: Some DNS providers require you to use '@' or leave the name")
                    print("      blank for the root domain, or just the subdomain part (e.g., 'app')")
                    print("      if your domain is 'app.example.com'.")
                    print("="*70)
                    print()
            else:
                print("Skipping CloudFront setup.")
                configure_website_hosting(s3, args.bucket, state, project_dir)
    else:
        # Configure as public website
        configure_website_hosting(s3, args.bucket, state, project_dir)

    # Print final URL
    state = load_state(project_dir)
    if state.get("domain"):
        print(f"\nSite URL: https://{state['domain']}")
    elif state.get("cloudfront_domain"):
        print(f"\nSite URL: https://{state['cloudfront_domain']}")
    elif state.get("s3_website_url"):
        print(f"\nSite URL: {state['s3_website_url']}")

    print("Done!")


if __name__ == "__main__":
    main()
