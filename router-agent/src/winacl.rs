//! The rule Windows private files must meet, kept free of Win32 calls so every platform tests it.
//!
//! Unix asks for a file owned by this user with mode 0600 inside a directory nobody else can write
//! (no group/other write bits). The Windows equivalent: the owner is this user or Administrators
//! (what an elevated process of this user creates files as; only an administrator can make a file
//! theirs, and administrators can take any file anyway, like root), a directory may also belong to
//! SYSTEM, and no allow entry in the DACL gives anyone else more than these bits. Entries this check
//! cannot read fail closed.

/// One DACL entry: (ACE type, ACE flags, access mask, SID bytes).
pub type Ace<'a> = (u8, u8, u32, &'a [u8]);

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Kind {
    File,
    Directory,
}

pub const ACCESS_ALLOWED_ACE_TYPE: u8 = 0;
const ACCESS_DENIED_ACE_TYPE: u8 = 1;
const ACCESS_DENIED_OBJECT_ACE_TYPE: u8 = 6;
const ACCESS_DENIED_CALLBACK_ACE_TYPE: u8 = 10;
const ACCESS_DENIED_CALLBACK_OBJECT_ACE_TYPE: u8 = 12;
pub const INHERIT_ONLY_ACE: u8 = 0x08;

const SYNCHRONIZE: u32 = 0x0010_0000;
const READ_CONTROL: u32 = 0x0002_0000;
const FILE_READ_ATTRIBUTES: u32 = 0x80;
/// What others may hold on a private file: see that it exists, nothing more (no data, no EAs).
const FILE_ALLOWED: u32 = SYNCHRONIZE | READ_CONTROL | FILE_READ_ATTRIBUTES;
/// What others must not hold on the directory: the Windows form of group/other write.
const DIRECTORY_FORBIDDEN: u32 = 0x2 // FILE_ADD_FILE
    | 0x4 // FILE_ADD_SUBDIRECTORY
    | 0x10 // FILE_WRITE_EA
    | 0x40 // FILE_DELETE_CHILD
    | 0x100 // FILE_WRITE_ATTRIBUTES
    | 0x1_0000 // DELETE
    | 0x4_0000 // WRITE_DAC
    | 0x8_0000 // WRITE_OWNER
    | 0x100_0000 // ACCESS_SYSTEM_SECURITY
    | 0x200_0000 // MAXIMUM_ALLOWED
    | 0x1000_0000 // GENERIC_ALL
    | 0x4000_0000; // GENERIC_WRITE

/// S-1-5-18 (LocalSystem).
pub const SYSTEM_SID: [u8; 12] = [1, 1, 0, 0, 0, 0, 0, 5, 18, 0, 0, 0];
/// S-1-5-32-544 (BUILTIN\Administrators).
pub const ADMINISTRATORS_SID: [u8; 16] = [1, 2, 0, 0, 0, 0, 0, 5, 32, 0, 0, 0, 0x20, 0x02, 0, 0];
/// S-1-3-4 (OWNER RIGHTS): stands for the owner, who is already checked.
pub const OWNER_RIGHTS_SID: [u8; 12] = [1, 1, 0, 0, 0, 0, 0, 3, 4, 0, 0, 0];

fn trusted(sid: &[u8], user: &[u8]) -> bool {
    sid == user || sid == SYSTEM_SID || sid == ADMINISTRATORS_SID || sid == OWNER_RIGHTS_SID
}

/// Whether an object with this owner and DACL is private to `user`. `dacl` None is a NULL DACL,
/// which grants everyone everything.
pub fn private(kind: Kind, owner: &[u8], dacl: Option<&[Ace]>, user: &[u8]) -> bool {
    let owner_ok = match kind {
        Kind::File => owner == user || owner == ADMINISTRATORS_SID,
        Kind::Directory => owner == user || owner == SYSTEM_SID || owner == ADMINISTRATORS_SID,
    };
    let Some(aces) = dacl else { return false };
    owner_ok
        && aces.iter().all(|&(ace_type, flags, mask, sid)| {
            if flags & INHERIT_ONLY_ACE != 0 {
                return true; // applies only to children, which are checked themselves
            }
            match ace_type {
                ACCESS_ALLOWED_ACE_TYPE => {
                    trusted(sid, user)
                        || match kind {
                            Kind::File => mask & !FILE_ALLOWED == 0,
                            Kind::Directory => mask & DIRECTORY_FORBIDDEN == 0,
                        }
                }
                ACCESS_DENIED_ACE_TYPE | ACCESS_DENIED_OBJECT_ACE_TYPE | ACCESS_DENIED_CALLBACK_ACE_TYPE | ACCESS_DENIED_CALLBACK_OBJECT_ACE_TYPE => true,
                _ => false,
            }
        })
}

#[cfg(test)]
mod tests {
    use super::*;

    const USER: [u8; 28] = [1, 5, 0, 0, 0, 0, 0, 5, 21, 0, 0, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 0xe9, 3, 0, 0];
    const OTHER: [u8; 28] = [1, 5, 0, 0, 0, 0, 0, 5, 21, 0, 0, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 0xea, 3, 0, 0];
    /// S-1-5-11 (Authenticated Users), S-1-1-0 (Everyone).
    const AUTHENTICATED: [u8; 12] = [1, 1, 0, 0, 0, 0, 0, 5, 11, 0, 0, 0];
    const EVERYONE: [u8; 12] = [1, 1, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0];
    const FULL: u32 = 0x1f01ff;
    const READ: u32 = 0x120089;
    const MODIFY: u32 = 0x1301bf;

    #[test]
    fn a_profile_default_acl_is_private() {
        // What files under %USERPROFILE% inherit: SYSTEM, Administrators and the user, full control.
        let aces: [Ace; 3] = [(0, 0x10, FULL, &SYSTEM_SID), (0, 0x10, FULL, &ADMINISTRATORS_SID), (0, 0x10, FULL, &USER)];
        assert!(private(Kind::File, &USER, Some(&aces), &USER));
        assert!(private(Kind::Directory, &USER, Some(&aces), &USER));
    }

    #[test]
    fn others_may_not_read_a_file_or_write_a_directory() {
        for other in [&OTHER[..], &AUTHENTICATED, &EVERYONE] {
            let read: [Ace; 2] = [(0, 0, FULL, &USER), (0, 0, READ, other)];
            assert!(!private(Kind::File, &USER, Some(&read), &USER));
            // Listing a directory is fine (like 0755); adding or deleting files is not.
            assert!(private(Kind::Directory, &USER, Some(&read), &USER));
            let modify: [Ace; 2] = [(0, 0, FULL, &USER), (0, 0, MODIFY, other)];
            assert!(!private(Kind::Directory, &USER, Some(&modify), &USER));
            let generic_write: [Ace; 1] = [(0, 0, 0x4000_0000, other)];
            assert!(!private(Kind::Directory, &USER, Some(&generic_write), &USER));
        }
        // Attributes and the ACL itself may be visible; the contents may not.
        let attributes: [Ace; 2] = [(0, 0, FULL, &USER), (0, 0, FILE_ALLOWED, &EVERYONE)];
        assert!(private(Kind::File, &USER, Some(&attributes), &USER));
    }

    #[test]
    fn owner_null_dacl_and_unknown_entries() {
        let mine: [Ace; 1] = [(0, 0, FULL, &USER)];
        assert!(!private(Kind::File, &OTHER, Some(&mine), &USER));
        // An elevated process of this user creates files owned by Administrators.
        assert!(private(Kind::File, &ADMINISTRATORS_SID, Some(&mine), &USER));
        assert!(!private(Kind::File, &SYSTEM_SID, Some(&mine), &USER));
        assert!(private(Kind::Directory, &ADMINISTRATORS_SID, Some(&mine), &USER));
        assert!(!private(Kind::Directory, &OTHER, Some(&mine), &USER));
        assert!(!private(Kind::File, &USER, None, &USER));
        // Deny entries only take access away; inherit-only entries apply to children.
        let deny: [Ace; 2] = [(0, 0, FULL, &USER), (1, 0, FULL, &EVERYONE)];
        assert!(private(Kind::File, &USER, Some(&deny), &USER));
        let inherit_only: [Ace; 2] = [(0, 0, FULL, &USER), (0, INHERIT_ONLY_ACE | 0x3, FULL, &EVERYONE)];
        assert!(private(Kind::File, &USER, Some(&inherit_only), &USER));
        // An allow entry this check cannot read (object, callback, compound) fails closed.
        for unknown in [4u8, 5, 9, 11] {
            let aces: [Ace; 2] = [(0, 0, FULL, &USER), (unknown, 0, FULL, &USER)];
            assert!(!private(Kind::File, &USER, Some(&aces), &USER));
        }
    }
}
