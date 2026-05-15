#!/usr/bin/env node
import "source-map-support/register";
import * as cdk from "aws-cdk-lib";
import { MetisSiteStack } from "../lib/metis-site-stack";

const app = new cdk.App();

const siteDomain = app.node.tryGetContext("siteDomain") as string;
const wwwSubdomain = app.node.tryGetContext("wwwSubdomain") as string;
const githubOwner = app.node.tryGetContext("githubOwner") as string;
const githubRepo = app.node.tryGetContext("githubRepo") as string;
const githubBranch =
  (app.node.tryGetContext("githubBranch") as string | undefined) ?? "main";

if (!siteDomain || !wwwSubdomain) {
  throw new Error("cdk.json must define siteDomain and wwwSubdomain");
}

// CloudFront certificates must live in us-east-1, so we pin the stack there.
// Putting S3 in the same region keeps everything in one stack with no
// cross-region replication shenanigans.
new MetisSiteStack(app, "MetisSiteStack", {
  env: {
    account: process.env.CDK_DEFAULT_ACCOUNT,
    region: "us-east-1",
  },
  siteDomain,
  wwwSubdomain,
  githubOwner,
  githubRepo,
  githubBranch,
  description: "Metis marketing site: S3 + CloudFront + Route 53 + OIDC role",
});
