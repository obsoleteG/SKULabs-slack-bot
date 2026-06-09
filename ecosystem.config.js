module.exports = {
  apps: [{
    name: "skustock-bot",
    script: "bots.py",
    interpreter: "/usr/bin/python3",
    watch: false,
    autorestart: true,
    env_file: ".env",
  }]
}
