/**
 * Mock data for MAIT CampusFix — DEMO ONLY.
 * These are illustrative figures and do NOT represent real MAIT complaint statistics.
 */

import type { Complaint, Student, Staff, StatusHistoryEntry } from '../types';

// ─── Demo current user ────────────────────────────────────────────────────────
export const MOCK_CURRENT_STUDENT: Student = {
  id: 'mock-stu-001',
  name: 'Ayush Sharma',
  rollNumber: '22BCE0001',
  branch: 'B.Tech CSE',
  year: 3,
};

// ─── Demo staff roster ────────────────────────────────────────────────────────
export const MOCK_STAFF: Staff[] = [
  { id: 'stf-001', name: 'Ramesh Kumar',   role: 'Electrician',    department: 'Maintenance',  phone: '98xxxxxxx1' },
  { id: 'stf-002', name: 'Suresh Verma',   role: 'Plumber',        department: 'Maintenance',  phone: '98xxxxxxx2' },
  { id: 'stf-003', name: 'Dinesh Yadav',   role: 'Civil Engineer', department: 'Civil Works',  phone: '98xxxxxxx3' },
  { id: 'stf-004', name: 'Priya Singh',    role: 'IT Technician',  department: 'IT / Network', phone: '98xxxxxxx4' },
  { id: 'stf-005', name: 'Meena Gupta',    role: 'Housekeeper',    department: 'Housekeeping', phone: '98xxxxxxx5' },
];

// ─── Helpers ──────────────────────────────────────────────────────────────────
const id = (n: number) => `CF-2024-${String(n).padStart(4, '0')}`;
const daysAgo = (d: number) => new Date(Date.now() - d * 86_400_000).toISOString();

/** Build a synthetic status history for seed complaints that reflect their current state. */
function pendingHistory(): StatusHistoryEntry[] {
  return []; // No transitions yet — complaint was just submitted
}

function inProgressHistory(submittedAt: string, staff: string): StatusHistoryEntry[] {
  // Simulate: was picked up 1 day after submission
  const pickedUpAt = new Date(new Date(submittedAt).getTime() + 86_400_000).toISOString();
  return [
    {
      from: 'pending',
      to: 'in-progress',
      changedAt: pickedUpAt,
      changedBy: staff,
      remarks: 'Complaint acknowledged and assigned.',
    },
  ];
}

function resolvedHistory(
  submittedAt: string,
  resolvedAt: string,
  staff: string,
  remarks: string,
): StatusHistoryEntry[] {
  const pickedUpAt = new Date(new Date(submittedAt).getTime() + 86_400_000).toISOString();
  return [
    {
      from: 'pending',
      to: 'in-progress',
      changedAt: pickedUpAt,
      changedBy: staff,
      remarks: 'Complaint acknowledged and work started.',
    },
    {
      from: 'in-progress',
      to: 'resolved',
      changedAt: resolvedAt,
      changedBy: staff,
      remarks,
    },
  ];
}

// ─── Seed complaints ──────────────────────────────────────────────────────────
export const SEED_COMPLAINTS: Complaint[] = [
  {
    id: id(1),
    title: 'Flickering lights in Lab C-204',
    description: 'The overhead fluorescent lights in Computer Lab C-204 have been flickering intermittently for the past week. It causes eye strain during long lab sessions.',
    building: 'Block C (Labs)',
    floor: '2nd Floor',
    roomNumber: 'C-204',
    category: 'Electrical',
    status: 'in-progress',
    submittedAt: daysAgo(9),
    upvotes: ['mock-stu-001', 'stu-002', 'stu-003'],
    assignedStaff: 'Ramesh Kumar',
    submittedBy: 'stu-002',
    overdueThresholdDays: 7,
    statusHistory: inProgressHistory(daysAgo(9), 'Ramesh Kumar'),
  },
  {
    id: id(2),
    title: 'Broken tap in Boys Washroom – Block A, 1st Floor',
    description: 'The cold-water tap near the entrance has been leaking continuously. Water is pooling on the floor, creating a slip hazard.',
    building: 'Block A',
    floor: '1st Floor',
    roomNumber: 'Washroom (Gents)',
    category: 'Plumbing',
    status: 'pending',
    submittedAt: daysAgo(5),
    upvotes: ['stu-004', 'stu-005'],
    submittedBy: 'stu-003',
    overdueThresholdDays: 7,
    statusHistory: pendingHistory(),
  },
  {
    id: id(3),
    title: 'Projector not working in MB-101',
    description: 'The ceiling-mounted projector in Lecture Hall MB-101 displays a blue screen. Faculty cannot conduct presentations or online classes.',
    building: 'Main Block (MB)',
    floor: 'Ground Floor',
    roomNumber: 'MB-101',
    category: 'Electrical',
    status: 'resolved',
    submittedAt: daysAgo(14),
    resolvedAt: daysAgo(3),
    upvotes: ['mock-stu-001', 'stu-006'],
    assignedStaff: 'Ramesh Kumar',
    resolutionRemarks: 'Replaced the HDMI signal board. Projector tested and confirmed working.',
    submittedBy: 'mock-stu-001',
    overdueThresholdDays: 7,
    statusHistory: resolvedHistory(
      daysAgo(14),
      daysAgo(3),
      'Ramesh Kumar',
      'Replaced the HDMI signal board. Projector tested and confirmed working.',
    ),
  },
  {
    id: id(4),
    title: 'Network Wi-Fi dead zone on Library 2nd Floor',
    description: 'Students in the reference section on the 2nd floor of the library get no Wi-Fi signal. Affects online research and NPTEL access.',
    building: 'Library Block',
    floor: '2nd Floor',
    roomNumber: 'Reference Section',
    category: 'IT / Network',
    status: 'pending',
    submittedAt: daysAgo(11),
    upvotes: ['mock-stu-001', 'stu-002', 'stu-007', 'stu-008'],
    submittedBy: 'stu-005',
    overdueThresholdDays: 7,
    statusHistory: pendingHistory(),
  },
  {
    id: id(5),
    title: 'Broken chairs in Seminar Hall – Block B',
    description: '6 out of 20 chairs in the seminar hall have broken arm rests or unstable legs. Risk of injury during events.',
    building: 'Block B',
    floor: 'Ground Floor',
    roomNumber: 'Seminar Hall',
    category: 'Furniture',
    status: 'pending',
    submittedAt: daysAgo(3),
    upvotes: ['stu-009'],
    submittedBy: 'mock-stu-001',
    overdueThresholdDays: 7,
    statusHistory: pendingHistory(),
  },
  {
    id: id(6),
    title: 'Ceiling water seepage in Admin corridor',
    description: 'Brown water stains appearing on the Admin Block corridor ceiling after every rain. Plaster is falling off in patches.',
    building: 'Administrative Block',
    floor: '1st Floor',
    roomNumber: 'Corridor near Room 110',
    category: 'Civil / Structural',
    status: 'in-progress',
    submittedAt: daysAgo(20),
    upvotes: ['stu-002', 'stu-010'],
    assignedStaff: 'Dinesh Yadav',
    submittedBy: 'stu-006',
    overdueThresholdDays: 7,
    statusHistory: inProgressHistory(daysAgo(20), 'Dinesh Yadav'),
  },
  {
    id: id(7),
    title: 'Fire extinguisher missing – Workshop Block',
    description: 'The fire extinguisher mount near the lathe machine section is empty. Last inspection sticker is from 18 months ago.',
    building: 'Workshop Block',
    floor: 'Ground Floor',
    roomNumber: 'Lathe Section',
    category: 'Fire Safety',
    status: 'pending',
    submittedAt: daysAgo(2),
    upvotes: ['mock-stu-001', 'stu-002', 'stu-003', 'stu-004', 'stu-005'],
    submittedBy: 'stu-007',
    overdueThresholdDays: 3,
    statusHistory: pendingHistory(),
  },
  {
    id: id(8),
    title: 'Washroom hygiene issue – Block C Ground Floor',
    description: 'The ladies washroom near Lab C-005 is not being cleaned regularly. Supplies (soap, tissue) are consistently missing.',
    building: 'Block C (Labs)',
    floor: 'Ground Floor',
    roomNumber: 'Washroom (Ladies)',
    category: 'Housekeeping',
    status: 'resolved',
    submittedAt: daysAgo(18),
    resolvedAt: daysAgo(10),
    upvotes: ['stu-011', 'stu-012'],
    assignedStaff: 'Meena Gupta',
    resolutionRemarks: 'Increased cleaning frequency to 3× daily. Soap dispensers installed.',
    submittedBy: 'stu-008',
    overdueThresholdDays: 7,
    feedback: {
      rating: 4,
      comment: 'Cleaning frequency has improved significantly. Soap dispensers are refilled.',
      submittedAt: daysAgo(9),
    },
    statusHistory: resolvedHistory(
      daysAgo(18),
      daysAgo(10),
      'Meena Gupta',
      'Increased cleaning frequency to 3× daily. Soap dispensers installed.',
    ),
  },
  {
    id: id(9),
    title: 'Short circuit tripping in Block A Server Room',
    description: 'UPS output circuit trips whenever the AC and servers are running simultaneously. Risk of data loss.',
    building: 'Block A',
    floor: '2nd Floor',
    roomNumber: 'Server Room A-205',
    category: 'Electrical',
    status: 'in-progress',
    submittedAt: daysAgo(6),
    upvotes: ['stu-004'],
    assignedStaff: 'Ramesh Kumar',
    submittedBy: 'stu-009',
    overdueThresholdDays: 5,
    statusHistory: inProgressHistory(daysAgo(6), 'Ramesh Kumar'),
  },
  {
    id: id(10),
    title: 'Canteen drinking water cooler not working',
    description: 'The water cooler in the canteen has been dispensing lukewarm water since last week. Summers are starting – students need cold water.',
    building: 'Canteen Block',
    floor: 'Ground Floor',
    roomNumber: 'Main Canteen Area',
    category: 'Electrical',
    status: 'pending',
    submittedAt: daysAgo(1),
    upvotes: ['mock-stu-001', 'stu-013', 'stu-014', 'stu-015'],
    submittedBy: 'stu-010',
    overdueThresholdDays: 7,
    statusHistory: pendingHistory(),
  },
];
