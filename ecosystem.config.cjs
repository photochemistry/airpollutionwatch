const fs = require("fs");
const path = require("path");

/** @type {import('pm2').StartOptions} */
const baseApp = {
  script: "scripts/pm2-start.sh",
  interpreter: "bash",
  autorestart: true,
  max_restarts: 10,
  min_uptime: "10s",
  restart_delay: 3000,
};

const apiRoot = __dirname;
const gridRoot =
  process.env.AIRPOLLUTIONWATCH_GRID_DIR ||
  path.resolve(__dirname, "../airpollutionwatch-grid");

/** @type {import('pm2').StartOptions[]} */
const apps = [
  {
    ...baseApp,
    name: "airpollutionwatch-api",
    cwd: apiRoot,
  },
];

if (fs.existsSync(gridRoot)) {
  apps.push({
    ...baseApp,
    name: "airpollutionwatch-grid",
    cwd: gridRoot,
  });
}

module.exports = { apps };
