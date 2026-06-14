/** @type {import('pm2').StartOptions[]} */
const apps = [
  {
    name: "airpollutionwatch-api",
    script: "scripts/pm2-start.sh",
    cwd: "/home/ubuntu/github/airpollutionwatch-api",
    interpreter: "bash",
    autorestart: true,
    max_restarts: 10,
    min_uptime: "10s",
    restart_delay: 3000,
  },
  {
    name: "airpollutionwatch-grid",
    script: "scripts/pm2-start.sh",
    cwd: "/home/ubuntu/github/airpollutionwatch-grid",
    interpreter: "bash",
    autorestart: true,
    max_restarts: 10,
    min_uptime: "10s",
    restart_delay: 3000,
  },
];

module.exports = { apps };
