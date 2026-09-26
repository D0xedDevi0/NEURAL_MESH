// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";

/// @title PomEscrow — Proof-of-Memory bond escrow (Base, USDC)
/// @notice Agents stake USDC behind a memory claim. The mesh's deterministic
///         settlement verdict drives the outcome:
///           * corroborated  -> releaseStake() returns the full bond
///           * falsified     -> settleSlash() moves the slashed amount to the
///                              challenger who caught the lie
///         Settlement is NOT an on-chain oracle: the owner is a thin relay that
///         publishes a verdict the mesh (``bonds.settlement_verdict``) already
///         produced off-chain. Anyone can recompute that verdict against public
///         mesh state, so the relay is auditable, not a judge.
contract PomEscrow is Ownable, ReentrancyGuard {
    /// @notice USDC (6 decimals). Raw units throughout — matches the mesh's
    ///         micro-USDC integer accounting (1 USDC == 1e6 units).
    IERC20 public immutable usdc;

    struct Bond {
        address staker;
        uint256 amount; // remaining USDC locked (raw 6-dec units)
        bool active;    // false once released / fully slashed
    }

    mapping(bytes32 => Bond) public bonds;

    event Staked(bytes32 indexed claimId, address indexed staker, uint256 amount);
    event Slashed(bytes32 indexed claimId, address indexed staker, address indexed challenger, uint256 amount);
    event Released(bytes32 indexed claimId, address indexed staker, uint256 amount);

    error ZeroAmount();
    error ZeroAddress();
    error SelfStakeOnly();
    error SelfSlash();
    error NotActive(bytes32 claimId);
    error ExceedsBond(uint256 remaining, uint256 requested);
    error StakerMismatch();

    constructor(address _usdc) Ownable(msg.sender) {
        if (_usdc == address(0)) revert ZeroAddress();
        usdc = IERC20(_usdc);
    }

    /// @notice Stake USDC behind a claim. Only the staker may stake their own
    ///         funds (msg.sender must equal `staker`) — the param is passed
    ///         explicitly to keep the ABI stable with the mesh's calldata builder.
    function escrowStake(bytes32 claimId, address staker, uint256 amountUsdc)
        external nonReentrant
    {
        if (staker != msg.sender) revert SelfStakeOnly();
        if (amountUsdc == 0) revert ZeroAmount();
        Bond storage b = bonds[claimId];
        // An active bond may only be topped up by its original staker — this
        // prevents a second party hijacking an unsettled bond by re-staking id.
        if (b.active && b.staker != staker) revert StakerMismatch();

        // Checks-effects-interactions: write state before the external transfer.
        b.staker = staker;
        b.amount += amountUsdc;
        b.active = true;

        require(usdc.transferFrom(staker, address(this), amountUsdc), "TRANSFER_FAIL");
        emit Staked(claimId, staker, amountUsdc);
    }

    /// @notice Falsified claim: slash `amountUsdc` from the staker's bond to the
    ///         challenger. Owner (settlement relay) only. Partial slashes are
    ///         supported — a remainder stays locked until released.
    function settleSlash(
        bytes32 claimId,
        address staker,
        address challenger,
        uint256 amountUsdc
    ) external onlyOwner nonReentrant {
        if (challenger == address(0)) revert ZeroAddress();
        if (challenger == staker) revert SelfSlash();
        if (amountUsdc == 0) revert ZeroAmount();

        Bond storage b = bonds[claimId];
        if (!b.active) revert NotActive(claimId);
        if (b.staker != staker) revert NotActive(claimId);
        if (amountUsdc > b.amount) revert ExceedsBond(b.amount, amountUsdc);

        b.amount -= amountUsdc;
        if (b.amount == 0) {
            b.active = false;
        }

        require(usdc.transfer(challenger, amountUsdc), "SLASH_TRANSFER_FAIL");
        emit Slashed(claimId, staker, challenger, amountUsdc);
    }

    /// @notice Corroborated claim: return the staker's remaining bond in full.
    ///         Owner (settlement relay) only — a staker cannot self-release while
    ///         a claim is live, otherwise slashing would be toothless.
    function releaseStake(bytes32 claimId, address staker)
        external onlyOwner nonReentrant
    {
        Bond storage b = bonds[claimId];
        if (!b.active) revert NotActive(claimId);
        if (b.staker != staker) revert NotActive(claimId);

        uint256 amt = b.amount;
        b.amount = 0;
        b.active = false;

        require(usdc.transfer(staker, amt), "RELEASE_TRANSFER_FAIL");
        emit Released(claimId, staker, amt);
    }

    /// @notice View a bond's live state (staker, remaining amount, active).
    function bondView(bytes32 claimId)
        external view returns (address staker, uint256 amount, bool active)
    {
        Bond storage b = bonds[claimId];
        return (b.staker, b.amount, b.active);
    }

    /// @notice Owner-only recovery of any ERC-20 accidentally sent here (not USDC).
    function recoverToken(address token, address to, uint256 amount) external onlyOwner {
        require(token != address(usdc), "USDC_PROTECTED");
        require(IERC20(token).transfer(to, amount), "RECOVER_FAIL");
    }
}
