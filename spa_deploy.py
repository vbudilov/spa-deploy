#!/usr/bin/env python3
"""Deploy a Vite/Yarn SPA project to S3, optionally fronted by CloudFront."""

import argparse
import json
import mimetypes
import os
import subprocess
import sys
import time
import uuid
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
    except ClientError:
        pass

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
    """Upload all files from the build output to S3."""
    output_path = Path(output_dir)
    files = [f for f in output_path.rglob("*") if f.is_file()]
    print(f"Uploading {len(files)} files to s3://{bucket_name}/...")

    for file_path in files:
        key = str(file_path.relative_to(output_path))
        content_type, _ = mimetypes.guess_type(str(file_path))
        if content_type is None:
            content_type = "application/octet-stream"

        extra_args = {"ContentType": content_type}

        # Set cache headers: long cache for hashed assets, short for html
        if file_path.suffix in (".html",):
            extra_args["CacheControl"] = "no-cache"
        elif "/assets/" in key or "\\assets\\" in key:
            extra_args["CacheControl"] = "public, max-age=31536000, immutable"

        s3.upload_file(str(file_path), bucket_name, key, ExtraArgs=extra_args)

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


def request_acm_certificate(session, domain: str, route53, zone_id: str, state: dict, project_dir: str) -> str:
    """Request an ACM certificate with DNS validation and wait for it to be issued."""
    # ACM must be in us-east-1 for CloudFront
    acm = session.client("acm", region_name="us-east-1")

    # Check if we already have a cert in state
    if state.get("acm_certificate_arn"):
        arn = state["acm_certificate_arn"]
        try:
            resp = acm.describe_certificate(CertificateArn=arn)
            status = resp["Certificate"]["Status"]
            if status == "ISSUED":
                print(f"ACM certificate already issued: {arn}")
                return arn
            print(f"Existing certificate status: {status}, will wait...")
        except ClientError:
            print("Previously tracked certificate not found, requesting new one...")

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
    for i in range(90):  # up to ~3 minutes
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

    # Create OAC
    caller_ref = str(uuid.uuid4())
    oac_name = f"{bucket_name}-oac"
    print("Creating Origin Access Control...")
    oac_resp = cf.create_origin_access_control(
        OriginAccessControlConfig={
            "Name": oac_name,
            "OriginAccessControlOriginType": "s3",
            "SigningBehavior": "always",
            "SigningProtocol": "sigv4",
        }
    )
    oac_id = oac_resp["OriginAccessControl"]["Id"]

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
            "ForwardedValues": None,  # not needed with CachePolicyId
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


def destroy_all(session, state: dict, project_dir: str):
    """Destroy all resources tracked in the state file, in reverse dependency order."""
    resources = state.get("created_resources", [])
    if not resources:
        sys.exit("Nothing to destroy — no resources tracked in state file.")

    print("The following resources will be destroyed:")
    if "route53_alias_record" in resources:
        print(f"  - Route53 alias record: {state.get('domain')}")
    if "route53_validation_record" in resources:
        print(f"  - Route53 ACM validation record")
    if "cloudfront_distribution" in resources:
        print(f"  - CloudFront distribution: {state.get('cloudfront_distribution_id')}")
    if "acm_certificate" in resources:
        print(f"  - ACM certificate: {state.get('acm_certificate_arn')}")
    if "s3_bucket" in resources:
        print(f"  - S3 bucket: {state.get('bucket_name')} (all objects will be deleted)")

    answer = input("\nAre you sure you want to destroy all resources? This cannot be undone. [y/N] ")
    if answer.strip().lower() not in ("y", "yes"):
        sys.exit("Aborted.")

    region = state.get("region", "us-east-1")

    # 1. Delete Route53 alias record
    if "route53_alias_record" in resources and state.get("domain") and state.get("route53_zone_id"):
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

    # 2. Delete Route53 ACM validation record
    if "route53_validation_record" in resources and state.get("acm_certificate_arn") and state.get("route53_zone_id"):
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

  %(prog)s --bucket my-app --skip-build --output ./dist
      Deploy a pre-built project from the ./dist directory.

  %(prog)s --bucket my-app --destroy
      Tear down all AWS resources tracked in the state file.

state tracking:
  All created resources are recorded in a spa_deploy.json file in the project
  directory. On subsequent deploys, existing resources are reused — only new
  files are uploaded and the CloudFront cache is invalidated. The --destroy
  flag reads this file to determine what to tear down.""",
    )
    parser.add_argument(
        "--bucket", required=True,
        help="S3 bucket name. The bucket is created if it does not exist. Used as the origin for CloudFront when --cloudfront is set.",
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
    args = parser.parse_args()

    if args.domain and not args.cloudfront:
        parser.error("--domain requires --cloudfront")

    project_dir = os.path.abspath(args.dir)
    if not os.path.isdir(project_dir):
        sys.exit(f"Project directory not found: {project_dir}")

    state = load_state(project_dir)

    # Destroy mode
    if args.destroy:
        session = boto3.Session(region_name=args.region)
        destroy_all(session, state, project_dir)
        return

    # Build
    if not args.skip_build:
        run_build(project_dir)

    # Detect output
    output_dir = args.output if args.output else detect_output_dir(project_dir)
    if not os.path.isdir(output_dir):
        sys.exit(f"Output directory not found: {output_dir}")

    # AWS session
    session = boto3.Session(region_name=args.region)
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
            answer = input("\nCreate a CloudFront distribution to front this S3 bucket? [y/N] ")
            if answer.strip().lower() in ("y", "yes"):
                # Handle domain + ACM + Route53 if requested
                if args.domain:
                    route53 = session.client("route53")
                    zone_id = state.get("route53_zone_id") or find_hosted_zone(route53, args.domain)
                    request_acm_certificate(session, args.domain, route53, zone_id, state, project_dir)
                    # Reload state after cert is saved
                    state = load_state(project_dir)

                setup_cloudfront(session, args.bucket, args.region, state, project_dir, domain=args.domain)

                # Create Route53 alias after distribution is created
                if args.domain:
                    state = load_state(project_dir)
                    create_domain_alias(route53, zone_id, args.domain, state["cloudfront_domain"], state, project_dir)
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
