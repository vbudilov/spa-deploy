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


def setup_cloudfront(session, bucket_name: str, region: str, state: dict, project_dir: str):
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

    print("Creating CloudFront distribution...")
    dist_resp = cf.create_distribution(
        DistributionConfig={
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
            "ViewerCertificate": {
                "CloudFrontDefaultCertificate": True,
            },
        }
    )

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


def main():
    parser = argparse.ArgumentParser(description="Deploy a Vite/Yarn SPA to AWS S3 (+ optional CloudFront)")
    parser.add_argument("--dir", default=".", help="Project directory (default: current directory)")
    parser.add_argument("--output", help="Build output directory (default: auto-detect dist/ or build/)")
    parser.add_argument("--bucket", required=True, help="S3 bucket name")
    parser.add_argument("--cloudfront", action="store_true", help="Front S3 with a CloudFront distribution")
    parser.add_argument("--region", default="us-east-1", help="AWS region (default: us-east-1)")
    parser.add_argument("--skip-build", action="store_true", help="Skip the build step")
    args = parser.parse_args()

    project_dir = os.path.abspath(args.dir)
    if not os.path.isdir(project_dir):
        sys.exit(f"Project directory not found: {project_dir}")

    state = load_state(project_dir)

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
                setup_cloudfront(session, args.bucket, args.region, state, project_dir)
            else:
                print("Skipping CloudFront setup.")
                configure_website_hosting(s3, args.bucket, state, project_dir)
    else:
        # Configure as public website
        configure_website_hosting(s3, args.bucket, state, project_dir)

    # Print final URL
    state = load_state(project_dir)
    if state.get("cloudfront_domain"):
        print(f"\nSite URL: https://{state['cloudfront_domain']}")
    elif state.get("s3_website_url"):
        print(f"\nSite URL: {state['s3_website_url']}")

    print("Done!")


if __name__ == "__main__":
    main()
