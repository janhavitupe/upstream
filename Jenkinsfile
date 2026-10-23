// Upstream CI/CD pipeline. Works on Windows and Linux Jenkins agents.
//
//   Install -> Syntax check -> Unit tests -> [End-to-end] -> [Live smoke test] -> [Deploy]
//
// Unit tests (every feature, offline, ~10 s) run on every build. End-to-end tests run every money
// flow against the real PayPal sandbox: nightly by themselves, or when RUN_E2E is ticked.
// Secrets live in Jenkins (Manage Jenkins -> Credentials), never in this repo. See docs/jenkins.md.

pipeline {
    agent any

    parameters {
        string(name: 'PYTHON', defaultValue: 'python',
               description: 'Python 3.11+ to use. On a Windows Jenkins service, give the full path ' +
                            '(e.g. C:\\Users\\you\\AppData\\Local\\Programs\\Python\\Python311\\python.exe).')
        booleanParam(name: 'RUN_E2E', defaultValue: false,
                     description: 'Run the end-to-end tests against the PayPal sandbox (they also run nightly).')
        booleanParam(name: 'RUN_SMOKE', defaultValue: false,
                     description: 'Also run the live PayPal sandbox smoke test (needs credentials).')
        string(name: 'SMOKE_SECTIONS', defaultValue: 'config oauth github',
               description: 'Smoke test sections, e.g. "config oauth invoices" (payouts move sandbox money).')
        booleanParam(name: 'DEPLOY', defaultValue: false,
                     description: 'Trigger a Render deploy after the tests pass (main branch only).')
    }

    options {
        timeout(time: 20, unit: 'MINUTES')
        buildDiscarder(logRotator(numToKeepStr: '20'))
        disableConcurrentBuilds()
    }

    // Jenkins on a laptop can't receive GitHub webhooks, so check the repo for new commits instead
    triggers {
        pollSCM('H/5 * * * *')   // new commits: unit tests
        cron('H 2 * * *')        // every night: unit + end-to-end tests
    }

    environment {
        PYTHONIOENCODING = 'utf-8'
        PIP_DISABLE_PIP_VERSION_CHECK = '1'
    }

    stages {
        stage('Install') {
            steps {
                run "\"${params.PYTHON}\" -m venv .venv-ci"
                run "${venvPython()} -m pip install --quiet -r requirements-dev.txt"
                run "${venvPython()} --version"
            }
        }

        stage('Syntax check') {
            steps {
                run "${venvPython()} -m compileall -q backend scripts tests"
            }
        }

        stage('Unit tests') {
            steps {
                run "${venvPython()} -m pytest --junitxml=reports/junit.xml"
            }
            post {
                always {
                    junit testResults: 'reports/junit.xml', allowEmptyResults: false
                }
            }
        }

        stage('End-to-end (PayPal sandbox)') {
            when { anyOf { expression { params.RUN_E2E }; expression { isNightly() } } }
            environment {
                UPSTREAM_E2E = '1'
                DEMO_MAINTAINER_EMAILS = 'maintainer-a@personal.example.com,maintainer-b@personal.example.com,maintainer-c@personal.example.com'
                DEMO_CLIENTS = 'Acme=acme-client@personal.example.com,Globex=globex-client@personal.example.com'
                DEMO_HOSPITAL_EMAIL = 'city-hospital@business.example.com'
            }
            steps {
                withCredentials([
                    string(credentialsId: 'paypal-client-id', variable: 'PAYPAL_CLIENT_ID'),
                    string(credentialsId: 'paypal-client-secret', variable: 'PAYPAL_CLIENT_SECRET'),
                    string(credentialsId: 'groq-api-key', variable: 'LLM_API_KEY'),
                ]) {
                    run "${venvPython()} -m pytest -m e2e --junitxml=reports/e2e.xml"
                }
            }
            post {
                always { junit testResults: 'reports/e2e.xml', allowEmptyResults: true }
            }
        }

        stage('Live smoke test') {
            when { expression { params.RUN_SMOKE } }
            steps {
                withCredentials([
                    string(credentialsId: 'paypal-client-id', variable: 'PAYPAL_CLIENT_ID'),
                    string(credentialsId: 'paypal-client-secret', variable: 'PAYPAL_CLIENT_SECRET'),
                    string(credentialsId: 'groq-api-key', variable: 'LLM_API_KEY'),
                    string(credentialsId: 'github-token', variable: 'GITHUB_TOKEN'),
                ]) {
                    run "${venvPython()} scripts/smoke_test.py ${params.SMOKE_SECTIONS}"
                }
            }
        }

        stage('Deploy to Render') {
            when {
                allOf {
                    expression { params.DEPLOY }
                    anyOf { branch 'main'; expression { env.GIT_BRANCH?.endsWith('/main') } }
                }
            }
            steps {
                withCredentials([string(credentialsId: 'render-deploy-hook', variable: 'DEPLOY_HOOK')]) {
                    // Python instead of curl so the same step works on Windows and Linux
                    run "${venvPython()} -c \"import os, urllib.request; " +
                        "r = urllib.request.urlopen(urllib.request.Request(os.environ['DEPLOY_HOOK'], method='POST')); " +
                        "print('Render deploy triggered:', r.status)\""
                }
            }
        }
    }

    post {
        success { echo 'All checks passed.' }
        failure { echo 'Pipeline failed: open the failed stage and the Tests tab for details.' }
        always  { archiveArtifacts artifacts: 'reports/*.xml', allowEmptyArchive: true }
    }
}

// Run a shell command on either kind of agent
def run(String cmd) {
    if (isUnix()) {
        sh cmd
    } else {
        bat cmd
    }
}

// True when the nightly timer (not a commit or a person) started this build
def isNightly() {
    return !currentBuild.getBuildCauses('hudson.triggers.TimerTrigger$TimerTriggerCause').isEmpty()
}

def venvPython() {
    return isUnix() ? '.venv-ci/bin/python' : '.venv-ci\\Scripts\\python.exe'
}
