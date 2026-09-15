// synthetic fixture
const queue = process.env.NOTIFY_QUEUE_URL;

function main() {
  console.log(`notifications reading ${queue}`);
}

main();
