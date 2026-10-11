import {DurableObject} from 'cloudflare:workers';
import {createUpdatesWorker, makeUpdatesObject} from './updates_worker.mjs';

export class UpdatesHub extends makeUpdatesObject(DurableObject) {}
export default createUpdatesWorker();
